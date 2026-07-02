"""Workspace-level lookup across multiple repository catalogs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_intel.catalog_store import CatalogStore
from code_intel.lookup import LookupHit, lookup

DEFAULT_WORKSPACE_LOOKUP_WORKERS = 4
DEFAULT_PARALLEL_WORKSPACE_LOOKUP_MIN_REPOS = 3


@dataclass(frozen=True, slots=True)
class WorkspaceLookupRepo:
    """Status for one repository searched by workspace lookup.

    Attributes:
        repo_path: Absolute repository path.
        label: Short repository label used in text output.
        searched: Whether a catalog was present and queried.
        hit_count: Number of ranked hits returned for the repository.
        error: Optional reason the repository could not be searched.
    """

    repo_path: str
    label: str
    searched: bool
    hit_count: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceLookupHit:
    """One lookup hit annotated with the repository that produced it.

    Attributes:
        repo_path: Absolute repository path.
        repo_label: Short repository label.
        kind: Source hit kind from the underlying lookup result.
        path: Cataloged repository-relative source path.
        line: One-based source line number.
        label: Human-readable match label.
        detail: Signature, line content, or supporting snippet.
        score: Lower scores rank earlier across the workspace.
        end_line: Optional one-based ending line for symbol hits.
    """

    repo_path: str
    repo_label: str
    kind: str
    path: str
    line: int
    label: str
    detail: str
    score: int
    end_line: int | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceLookupResult:
    """Lookup result spanning multiple repository catalogs.

    Attributes:
        query: User query string.
        repos: Per-repository search status.
        hits: Combined, ranked workspace hits.
    """

    query: str
    repos: list[WorkspaceLookupRepo]
    hits: list[WorkspaceLookupHit]


@dataclass(frozen=True, slots=True)
class _WorkspaceLookupTask:
    repo_index: int
    repo_path: Path
    store: CatalogStore | None
    query: str
    limit: int
    symbol_limit: int | None
    file_limit: int | None
    text_limit: int | None
    fallback_text_limit: int | None
    context_lines: int
    include_tests: bool


@dataclass(frozen=True, slots=True)
class _WorkspaceLookupTaskResult:
    repo: WorkspaceLookupRepo
    hits: list[WorkspaceLookupHit]


def workspace_lookup(
    repo_paths: list[str | Path],
    query: str,
    *,
    limit: int = 20,
    per_repo_limit: int | None = None,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    fallback_text_limit: int | None = None,
    context_lines: int = 0,
    include_tests: bool = True,
    stores_by_repo: dict[str, CatalogStore] | None = None,
) -> WorkspaceLookupResult:
    """Search several repository catalogs and merge ranked hits.

    Args:
        repo_paths: Repository roots whose `.code-intel/catalog.sqlite` files
            should be searched.
        query: Symbol or text query.
        limit: Maximum combined hits to return.
        per_repo_limit: Maximum hits to request from each repository. Defaults
            to the combined limit.
        symbol_limit: Optional per-repository symbol hit limit.
        file_limit: Optional per-repository file path hit limit.
        text_limit: Optional per-repository text hit limit.
        fallback_text_limit: Optional per-repository text hit budget used only
            when normal text search is disabled and source/file lookup finds no
            workspace hits.
        context_lines: Number of text context lines to include.
        include_tests: Whether test files may be returned as lookup hits.
        stores_by_repo: Optional pre-opened catalog stores keyed by absolute
            repository path. Used by serial workspace flows to avoid repeated
            SQLite connection setup.

    Returns:
        Combined workspace lookup result with per-repository status.

    Raises:
        ValueError: If no repository is supplied or the query is empty.
    """
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must not be empty")
    if not repo_paths:
        raise ValueError("at least one repository path is required")

    repo_roots = [_resolve_repo_root(repo_path) for repo_path in repo_paths]
    bounded_limit = max(1, min(limit, 100))
    bounded_per_repo_limit = _candidate_limit(limit=per_repo_limit or bounded_limit, include_tests=include_tests)
    repos, hits = _run_workspace_lookup(
        repo_roots,
        stripped,
        limit=bounded_per_repo_limit,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=text_limit,
        fallback_text_limit=None,
        context_lines=context_lines,
        include_tests=include_tests,
        stores_by_repo=stores_by_repo,
    )
    fallback_budget = max(0, min(fallback_text_limit, 100)) if fallback_text_limit is not None else 0
    if not hits and fallback_budget > 0:
        repos, hits = _run_workspace_lookup(
            repo_roots,
            stripped,
            limit=bounded_per_repo_limit,
            symbol_limit=0,
            file_limit=0,
            text_limit=fallback_budget,
            fallback_text_limit=None,
            context_lines=context_lines,
            include_tests=include_tests,
            stores_by_repo=stores_by_repo,
        )

    return WorkspaceLookupResult(
        query=stripped,
        repos=repos,
        hits=sorted(hits, key=lambda hit: (hit.score, hit.repo_label, hit.path, hit.line, hit.kind))[:bounded_limit],
    )


def workspace_lookup_to_dict(result: WorkspaceLookupResult) -> dict[str, Any]:
    """Serialize workspace lookup results for CLI and MCP responses.

    Args:
        result: Workspace lookup result.

    Returns:
        JSON-serializable dictionary.
    """
    return {
        "query": result.query,
        "repos": [
            {
                "repo_path": repo.repo_path,
                "label": repo.label,
                "searched": repo.searched,
                "hit_count": repo.hit_count,
                "error": repo.error,
            }
            for repo in result.repos
        ],
        "count": len(result.hits),
        "hits": [
            {
                "repo_path": hit.repo_path,
                "repo_label": hit.repo_label,
                "kind": hit.kind,
                "path": hit.path,
                "line": hit.line,
                "label": hit.label,
                "detail": hit.detail,
                "score": hit.score,
            }
            for hit in result.hits
        ],
    }


def workspace_selected_paths_by_repo(result: WorkspaceLookupResult) -> dict[str, set[str]]:
    """Return selected source paths grouped by repository path.

    Args:
        result: Workspace lookup result.

    Returns:
        Mapping from absolute repository path to cataloged source paths.
    """
    grouped: dict[str, set[str]] = {}
    for hit in result.hits:
        grouped.setdefault(hit.repo_path, set()).add(hit.path)
    return grouped


def _workspace_hits(
    hits: list[LookupHit],
    *,
    repo_path: Path,
    repo_label: str,
    repo_index: int,
) -> list[WorkspaceLookupHit]:
    return [
        WorkspaceLookupHit(
            repo_path=str(repo_path),
            repo_label=repo_label,
            kind=hit.kind,
            path=hit.path,
            line=hit.line,
            label=hit.label,
            detail=hit.detail,
            score=hit.score + repo_index,
            end_line=hit.end_line,
        )
        for hit in hits
    ]


def _run_workspace_lookup(
    repo_paths: list[Path],
    query: str,
    *,
    limit: int,
    symbol_limit: int | None,
    file_limit: int | None,
    text_limit: int | None,
    fallback_text_limit: int | None,
    context_lines: int,
    include_tests: bool,
    stores_by_repo: dict[str, CatalogStore] | None,
) -> tuple[list[WorkspaceLookupRepo], list[WorkspaceLookupHit]]:
    use_supplied_stores = len(repo_paths) < DEFAULT_PARALLEL_WORKSPACE_LOOKUP_MIN_REPOS
    tasks = [
        _WorkspaceLookupTask(
            repo_index=repo_index,
            repo_path=repo_path,
            store=stores_by_repo.get(str(repo_path)) if stores_by_repo and use_supplied_stores else None,
            query=query,
            limit=limit,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            fallback_text_limit=fallback_text_limit,
            context_lines=context_lines,
            include_tests=include_tests,
        )
        for repo_index, repo_path in enumerate(repo_paths)
    ]
    if len(tasks) < DEFAULT_PARALLEL_WORKSPACE_LOOKUP_MIN_REPOS:
        results = [_lookup_workspace_repo(tasks[0])]
        results.extend(_lookup_workspace_repo(task) for task in tasks[1:])
    else:
        workers = min(DEFAULT_WORKSPACE_LOOKUP_WORKERS, len(tasks))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_lookup_workspace_repo, tasks))

    repos = [result.repo for result in results]
    hits: list[WorkspaceLookupHit] = []
    for result in results:
        hits.extend(result.hits)
    return repos, hits


def _lookup_workspace_repo(task: _WorkspaceLookupTask) -> _WorkspaceLookupTaskResult:
    repo_path = task.repo_path
    repo_label = _repo_label(repo_path)
    store = task.store or CatalogStore.for_repo(repo_path, reuse_connection=True)
    if not store.has_catalog():
        repo = WorkspaceLookupRepo(
            repo_path=str(repo_path),
            label=repo_label,
            searched=False,
            error=f"No catalog found at {store.database_path}. Run `code-intel scan {repo_path}` first.",
        )
        return _WorkspaceLookupTaskResult(repo=repo, hits=[])

    result = lookup(
        repo_path,
        store,
        task.query,
        limit=task.limit,
        symbol_limit=task.symbol_limit,
        file_limit=task.file_limit,
        text_limit=task.text_limit,
        fallback_text_limit=task.fallback_text_limit,
        context_lines=task.context_lines,
        include_tests=task.include_tests,
    )
    repo = WorkspaceLookupRepo(
        repo_path=str(repo_path),
        label=repo_label,
        searched=True,
        hit_count=len(result.hits),
    )
    hits = _workspace_hits(result.hits, repo_path=repo_path, repo_label=repo_label, repo_index=task.repo_index)
    return _WorkspaceLookupTaskResult(repo=repo, hits=hits)


def _resolve_repo_root(repo_path: str | Path) -> Path:
    if isinstance(repo_path, Path) and repo_path.is_absolute():
        return repo_path
    return Path(repo_path).expanduser().resolve()


def _repo_label(repo_path: Path) -> str:
    parent = repo_path.parent.name
    name = repo_path.name
    if name == "src" and parent:
        return f"{parent}/{name}"
    return name


def _candidate_limit(*, limit: int, include_tests: bool) -> int:
    bounded_limit = max(1, min(limit, 100))
    if include_tests:
        return bounded_limit
    return min(100, max(bounded_limit, bounded_limit * 5 + 10))
