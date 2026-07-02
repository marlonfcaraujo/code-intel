"""Exact source reference search built on the catalog text index."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from code_intel.catalog_store import CatalogStore
from code_intel.models import TextMatch
from code_intel.workspace import WorkspaceLookupRepo

ReferenceKind = Literal["definition", "reference"]
DEFAULT_REFERENCE_LIMIT = 50
DEFAULT_REFERENCE_CONTEXT_LINES = 1
MAX_REFERENCE_CANDIDATES = 500


@dataclass(frozen=True, slots=True)
class ReferenceMatch:
    """One exact source reference match.

    Attributes:
        repo_path: Absolute repository path.
        repo_label: Short repository label.
        kind: Whether the match is a definition or non-definition reference.
        symbol_kind: Catalog symbol kind for definition matches.
        path: Cataloged repository-relative file path.
        line: One-based source line number.
        content: Matching source line.
        snippet: Bounded source snippet around the matching line.
        start_line: One-based first snippet line.
        end_line: One-based final snippet line.
        score: Lower scores rank earlier.
    """

    repo_path: str
    repo_label: str
    kind: ReferenceKind
    symbol_kind: str
    path: str
    line: int
    content: str
    snippet: str
    start_line: int
    end_line: int
    score: int


@dataclass(frozen=True, slots=True)
class ReferenceFileSummary:
    """Compact summary of references in one file.

    Attributes:
        repo_path: Absolute repository path.
        repo_label: Short repository label.
        path: Cataloged repository-relative file path.
        match_count: Total matches represented for the file.
        definition_count: Definition matches represented for the file.
        reference_count: Non-definition references represented for the file.
        first_line: Earliest matching line.
        last_line: Latest matching line.
        sample_content: First matching source line for quick orientation.
    """

    repo_path: str
    repo_label: str
    path: str
    match_count: int
    definition_count: int
    reference_count: int
    first_line: int
    last_line: int
    sample_content: str


@dataclass(frozen=True, slots=True)
class ReferenceResult:
    """Exact source reference result for one repository.

    Attributes:
        query: Identifier or text fragment searched.
        repo_path: Absolute repository path.
        candidate_matches: Text-index matches considered before exact filtering.
        selected_files: Number of distinct files represented in matches.
        estimated_tokens: Directional token estimate for returned snippets.
        summary_tokens: Directional token estimate for file-level summaries.
        file_summaries: Compact grouped summaries for matched files.
        matches: Exact reference matches.
    """

    query: str
    repo_path: str
    candidate_matches: int
    selected_files: int
    estimated_tokens: int
    summary_tokens: int
    file_summaries: list[ReferenceFileSummary]
    matches: list[ReferenceMatch]


@dataclass(frozen=True, slots=True)
class WorkspaceReferenceResult:
    """Exact source reference result across several repositories.

    Attributes:
        query: Identifier or text fragment searched.
        repos: Per-repository search status.
        candidate_matches: Text-index matches considered before exact filtering.
        selected_files: Number of distinct repo/file pairs represented.
        estimated_tokens: Directional token estimate for returned snippets.
        summary_tokens: Directional token estimate for file-level summaries.
        file_summaries: Compact grouped summaries for matched files.
        matches: Exact reference matches across repositories.
    """

    query: str
    repos: list[WorkspaceLookupRepo]
    candidate_matches: int
    selected_files: int
    estimated_tokens: int
    summary_tokens: int
    file_summaries: list[ReferenceFileSummary]
    matches: list[ReferenceMatch]


def find_references(
    repo_path: str | Path,
    store: CatalogStore,
    query: str,
    *,
    limit: int = DEFAULT_REFERENCE_LIMIT,
    context_lines: int = DEFAULT_REFERENCE_CONTEXT_LINES,
    include_definitions: bool = True,
    ignore_case: bool = False,
) -> ReferenceResult:
    """Find exact source references in one repository.

    Args:
        repo_path: Repository root to search.
        store: Catalog store for the repository.
        query: Identifier or text fragment to find.
        limit: Maximum exact references to return.
        context_lines: Lines before and after each reference.
        include_definitions: Whether definition lines should be returned.
        ignore_case: Whether exact filtering should ignore case.

    Returns:
        Exact reference result with bounded source snippets.

    Raises:
        RuntimeError: If the catalog lacks text search support.
        ValueError: If the query is empty.
    """
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must not be empty")

    repo_root = Path(repo_path).expanduser().resolve()
    bounded_limit = max(1, min(limit, 200))
    bounded_context = max(0, min(context_lines, 10))
    candidate_limit = max(100, min(MAX_REFERENCE_CANDIDATES, bounded_limit * 20))
    pattern = _reference_pattern(stripped, ignore_case=ignore_case)
    candidates = store.search_text(stripped, limit=candidate_limit, context_lines=bounded_context)

    matches: list[ReferenceMatch] = []
    symbol_cache: dict[str, list[dict[str, Any]]] = {}
    for index, candidate in enumerate(candidates):
        if not pattern.search(candidate.content):
            continue
        kind, symbol_kind = _classify_match(
            store,
            symbol_cache,
            candidate,
            stripped,
            ignore_case=ignore_case,
        )
        if kind == "definition" and not include_definitions:
            continue
        matches.append(
            _reference_match(
                repo_root=repo_root,
                repo_label=_repo_label(repo_root),
                match=candidate,
                kind=kind,
                symbol_kind=symbol_kind,
                score=index if kind == "definition" else 1000 + index,
            )
        )

    ranked_matches = sorted(matches, key=lambda match: (match.score, match.path, match.line))[:bounded_limit]

    file_summaries = _file_summaries(ranked_matches)
    return ReferenceResult(
        query=stripped,
        repo_path=str(repo_root),
        candidate_matches=len(candidates),
        selected_files=len({match.path for match in ranked_matches}),
        estimated_tokens=_estimate_tokens("\n".join(match.snippet for match in ranked_matches)),
        summary_tokens=_estimate_summary_tokens(file_summaries),
        file_summaries=file_summaries,
        matches=ranked_matches,
    )


def workspace_references(
    repo_paths: list[str | Path],
    query: str,
    *,
    limit: int = DEFAULT_REFERENCE_LIMIT,
    per_repo_limit: int | None = None,
    context_lines: int = DEFAULT_REFERENCE_CONTEXT_LINES,
    include_definitions: bool = True,
    ignore_case: bool = False,
    stores_by_repo: dict[str, CatalogStore] | None = None,
) -> WorkspaceReferenceResult:
    """Find exact source references across several repository catalogs.

    Args:
        repo_paths: Repository roots to search.
        query: Identifier or text fragment to find.
        limit: Maximum combined references to return.
        per_repo_limit: Maximum references requested from each repository.
        context_lines: Lines before and after each reference.
        include_definitions: Whether definition lines should be returned.
        ignore_case: Whether exact filtering should ignore case.
        stores_by_repo: Optional pre-opened catalog stores keyed by absolute
            repository path. Used by MCP and benchmark flows to avoid repeated
            SQLite connection setup.

    Returns:
        Workspace reference result with per-repository status.

    Raises:
        ValueError: If no repository path or query is supplied.
    """
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must not be empty")
    if not repo_paths:
        raise ValueError("at least one repository path is required")

    bounded_limit = max(1, min(limit, 200))
    bounded_per_repo_limit = max(1, min(per_repo_limit or bounded_limit, 200))
    repos: list[WorkspaceLookupRepo] = []
    matches: list[ReferenceMatch] = []
    candidate_matches = 0

    for repo_index, raw_repo_path in enumerate(repo_paths):
        repo_root = Path(raw_repo_path).expanduser().resolve()
        repo_label = _repo_label(repo_root)
        store = stores_by_repo.get(str(repo_root)) if stores_by_repo else CatalogStore.for_repo(repo_root)
        if not store.has_catalog():
            repos.append(
                WorkspaceLookupRepo(
                    repo_path=str(repo_root),
                    label=repo_label,
                    searched=False,
                    error=f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first.",
                )
            )
            continue

        result = find_references(
            repo_root,
            store,
            stripped,
            limit=bounded_per_repo_limit,
            context_lines=context_lines,
            include_definitions=include_definitions,
            ignore_case=ignore_case,
        )
        candidate_matches += result.candidate_matches
        repos.append(
            WorkspaceLookupRepo(
                repo_path=str(repo_root),
                label=repo_label,
                searched=True,
                hit_count=len(result.matches),
            )
        )
        matches.extend(_workspace_scored_matches(result.matches, repo_index=repo_index))

    ranked_matches = sorted(matches, key=lambda match: (match.score, match.repo_label, match.path, match.line))[
        :bounded_limit
    ]
    file_summaries = _file_summaries(ranked_matches)
    return WorkspaceReferenceResult(
        query=stripped,
        repos=repos,
        candidate_matches=candidate_matches,
        selected_files=len({(match.repo_path, match.path) for match in ranked_matches}),
        estimated_tokens=_estimate_tokens("\n".join(match.snippet for match in ranked_matches)),
        summary_tokens=_estimate_summary_tokens(file_summaries),
        file_summaries=file_summaries,
        matches=ranked_matches,
    )


def reference_result_to_dict(result: ReferenceResult, *, summary_only: bool = False) -> dict[str, Any]:
    """Serialize a single-repository reference result.

    Args:
        result: Reference result to serialize.
        summary_only: When true, omit per-line matches and return compact
            file-level summaries only.

    Returns:
        JSON-serializable dictionary.
    """
    return {
        "query": result.query,
        "repo_path": result.repo_path,
        "candidate_matches": result.candidate_matches,
        "selected_files": result.selected_files,
        "estimated_tokens": result.summary_tokens if summary_only else result.estimated_tokens,
        "snippet_tokens": result.estimated_tokens,
        "summary_tokens": result.summary_tokens,
        "summary_only": summary_only,
        "count": len(result.matches),
        "file_summaries": [_file_summary_to_dict(summary) for summary in result.file_summaries],
        "matches": [] if summary_only else [_reference_match_to_dict(match) for match in result.matches],
    }


def workspace_reference_result_to_dict(
    result: WorkspaceReferenceResult,
    *,
    summary_only: bool = False,
) -> dict[str, Any]:
    """Serialize a workspace reference result.

    Args:
        result: Workspace reference result to serialize.
        summary_only: When true, omit per-line matches and return compact
            file-level summaries only.

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
        "candidate_matches": result.candidate_matches,
        "selected_files": result.selected_files,
        "estimated_tokens": result.summary_tokens if summary_only else result.estimated_tokens,
        "snippet_tokens": result.estimated_tokens,
        "summary_tokens": result.summary_tokens,
        "summary_only": summary_only,
        "count": len(result.matches),
        "file_summaries": [_file_summary_to_dict(summary) for summary in result.file_summaries],
        "matches": [] if summary_only else [_reference_match_to_dict(match) for match in result.matches],
    }


def reference_selected_paths(result: ReferenceResult) -> set[str]:
    """Return selected source paths from a reference result.

    Args:
        result: Reference result.

    Returns:
        Cataloged source paths represented by matches.
    """
    return {match.path for match in result.matches}


def workspace_reference_selected_paths(result: WorkspaceReferenceResult) -> dict[str, set[str]]:
    """Return selected source paths grouped by repository.

    Args:
        result: Workspace reference result.

    Returns:
        Mapping from absolute repository path to selected cataloged paths.
    """
    grouped: dict[str, set[str]] = {}
    for match in result.matches:
        grouped.setdefault(match.repo_path, set()).add(match.path)
    return grouped


def workspace_reference_tokens_by_repo(result: WorkspaceReferenceResult) -> dict[str, int]:
    """Return estimated snippet tokens grouped by repository.

    Args:
        result: Workspace reference result.

    Returns:
        Mapping from absolute repository path to estimated returned tokens.
    """
    content_by_repo: dict[str, list[str]] = {}
    for match in result.matches:
        content_by_repo.setdefault(match.repo_path, []).append(match.snippet)
    return {repo_path: _estimate_tokens("\n".join(snippets)) for repo_path, snippets in content_by_repo.items()}


def workspace_reference_summary_tokens_by_repo(result: WorkspaceReferenceResult) -> dict[str, int]:
    """Return estimated file-summary tokens grouped by repository.

    Args:
        result: Workspace reference result.

    Returns:
        Mapping from absolute repository path to estimated returned summary tokens.
    """
    summaries_by_repo: dict[str, list[ReferenceFileSummary]] = {}
    for summary in result.file_summaries:
        summaries_by_repo.setdefault(summary.repo_path, []).append(summary)
    return {repo_path: _estimate_summary_tokens(summaries) for repo_path, summaries in summaries_by_repo.items()}


def workspace_reference_counts_by_repo(result: WorkspaceReferenceResult) -> dict[str, int]:
    """Return reference counts grouped by repository.

    Args:
        result: Workspace reference result.

    Returns:
        Mapping from absolute repository path to returned reference counts.
    """
    counts: dict[str, int] = {}
    for match in result.matches:
        counts[match.repo_path] = counts.get(match.repo_path, 0) + 1
    return counts


def _reference_pattern(query: str, *, ignore_case: bool) -> re.Pattern[str]:
    flags = re.IGNORECASE if ignore_case else 0
    escaped = re.escape(query)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", query):
        return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", flags)
    return re.compile(escaped, flags)


def _classify_match(
    store: CatalogStore,
    symbol_cache: dict[str, list[dict[str, Any]]],
    match: TextMatch,
    query: str,
    *,
    ignore_case: bool,
) -> tuple[ReferenceKind, str]:
    symbol_rows = symbol_cache.setdefault(match.path, [dict(row) for row in store.symbols_for_file(match.path)])
    for row in symbol_rows:
        if int(row["line"]) != match.line:
            continue
        if _symbol_matches_query(row, query, ignore_case=ignore_case):
            return "definition", str(row.get("kind", ""))
    if _line_defines_query(match.content, query, ignore_case=ignore_case):
        return "definition", "assignment"
    return "reference", ""


def _symbol_matches_query(row: dict[str, Any], query: str, *, ignore_case: bool) -> bool:
    candidates = [
        str(row.get("name", "")),
        str(row.get("qualified_name", "")).rsplit(".", maxsplit=1)[-1],
    ]
    if ignore_case:
        normalized = query.casefold()
        return any(candidate.casefold() == normalized for candidate in candidates)
    return query in candidates


def _line_defines_query(content: str, query: str, *, ignore_case: bool) -> bool:
    escaped = re.escape(query)
    flags = re.IGNORECASE if ignore_case else 0
    patterns = [
        rf"^\s*(?:export\s+)?(?:class|function|def|const|let|var|type|interface)\s+{escaped}(?![A-Za-z0-9_])",
        rf"^\s*{escaped}\s*(?::|=)",
    ]
    return any(re.search(pattern, content, flags) for pattern in patterns)


def _reference_match(
    *,
    repo_root: Path,
    repo_label: str,
    match: TextMatch,
    kind: ReferenceKind,
    symbol_kind: str,
    score: int,
) -> ReferenceMatch:
    return ReferenceMatch(
        repo_path=str(repo_root),
        repo_label=repo_label,
        kind=kind,
        symbol_kind=symbol_kind,
        path=match.path,
        line=match.line,
        content=match.content,
        snippet=match.snippet,
        start_line=match.start_line,
        end_line=match.end_line,
        score=score,
    )


def _workspace_scored_matches(matches: list[ReferenceMatch], *, repo_index: int) -> list[ReferenceMatch]:
    return [
        ReferenceMatch(
            repo_path=match.repo_path,
            repo_label=match.repo_label,
            kind=match.kind,
            symbol_kind=match.symbol_kind,
            path=match.path,
            line=match.line,
            content=match.content,
            snippet=match.snippet,
            start_line=match.start_line,
            end_line=match.end_line,
            score=match.score + repo_index,
        )
        for match in matches
    ]


def _file_summaries(matches: list[ReferenceMatch]) -> list[ReferenceFileSummary]:
    grouped: dict[tuple[str, str], list[ReferenceMatch]] = {}
    for match in matches:
        grouped.setdefault((match.repo_path, match.path), []).append(match)

    summaries: list[ReferenceFileSummary] = []
    for (_repo_path, _path), file_matches in grouped.items():
        ordered = sorted(file_matches, key=lambda match: match.line)
        first_match = ordered[0]
        summaries.append(
            ReferenceFileSummary(
                repo_path=first_match.repo_path,
                repo_label=first_match.repo_label,
                path=first_match.path,
                match_count=len(ordered),
                definition_count=sum(1 for match in ordered if match.kind == "definition"),
                reference_count=sum(1 for match in ordered if match.kind == "reference"),
                first_line=ordered[0].line,
                last_line=ordered[-1].line,
                sample_content=ordered[0].content,
            )
        )
    return sorted(summaries, key=lambda summary: (summary.repo_label, summary.path, summary.first_line))


def _reference_match_to_dict(match: ReferenceMatch) -> dict[str, Any]:
    return {
        "repo_path": match.repo_path,
        "repo_label": match.repo_label,
        "kind": match.kind,
        "symbol_kind": match.symbol_kind,
        "path": match.path,
        "line": match.line,
        "content": match.content,
        "snippet": match.snippet,
        "start_line": match.start_line,
        "end_line": match.end_line,
        "score": match.score,
    }


def _file_summary_to_dict(summary: ReferenceFileSummary) -> dict[str, Any]:
    return {
        "repo_path": summary.repo_path,
        "repo_label": summary.repo_label,
        "path": summary.path,
        "match_count": summary.match_count,
        "definition_count": summary.definition_count,
        "reference_count": summary.reference_count,
        "first_line": summary.first_line,
        "last_line": summary.last_line,
        "sample_content": summary.sample_content,
    }


def _repo_label(repo_path: Path) -> str:
    parent = repo_path.parent.name
    name = repo_path.name
    if name == "src" and parent:
        return f"{parent}/{name}"
    return name


def _estimate_tokens(content: str) -> int:
    return max(0, (len(content) + 3) // 4)


def _estimate_summary_tokens(summaries: list[ReferenceFileSummary]) -> int:
    lines = [
        f"{summary.repo_label} {summary.path}:{summary.first_line}-{summary.last_line} "
        f"matches={summary.match_count} definitions={summary.definition_count} "
        f"sample={summary.sample_content}"
        for summary in summaries
    ]
    return _estimate_tokens("\n".join(lines))
