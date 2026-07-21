"""Compact source context packs built from ranked lookup hits."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_intel.catalog_store import CatalogStore
from code_intel.lookup import lookup
from code_intel.savings import estimate_saved_tokens_for_context_pack
from code_intel.secret_filter import redact_source_text
from code_intel.workspace import workspace_lookup

DEFAULT_CONTEXT_PACK_LIMIT = 10
DEFAULT_CONTEXT_PACK_MAX_FILES = 5
DEFAULT_CONTEXT_PACK_LINES = 4
DEFAULT_CONTEXT_PACK_MAX_LINES_PER_FILE = 80


@dataclass(frozen=True, slots=True)
class ContextPackHit:
    """Lookup hit selected for a context pack.

    Attributes:
        repo_path: Absolute repository path.
        repo_label: Short repository label.
        kind: Lookup hit kind.
        path: Cataloged repository-relative file path.
        line: One-based source line number.
        label: Human-readable hit label.
        score: Lower scores ranked earlier in lookup.
        end_line: Optional one-based ending line for symbol hits.
    """

    repo_path: str
    repo_label: str
    kind: str
    path: str
    line: int
    label: str
    score: int
    end_line: int | None = None


@dataclass(frozen=True, slots=True)
class ContextSnippet:
    """Bounded source snippet selected for a context pack.

    Attributes:
        repo_path: Absolute repository path.
        repo_label: Short repository label.
        path: Cataloged repository-relative file path.
        start_line: One-based first line included.
        end_line: One-based final line included.
        line_count: Total line count for the file.
        hit_lines: Hit lines represented by the snippet.
        content: Source content for the bounded line range.
    """

    repo_path: str
    repo_label: str
    path: str
    start_line: int
    end_line: int
    line_count: int
    hit_lines: list[int]
    content: str


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Compact source context selected from ranked lookup hits.

    Attributes:
        query: Search query used to build the context pack.
        repo_paths: Repository roots searched.
        hit_count: Number of ranked hits considered.
        candidate_files: Number of cataloged files in searched repositories.
        selected_files: Number of distinct files represented by snippets.
        selected_lines: Number of source lines returned.
        estimated_tokens: Directional token estimate for snippet content.
        estimated_saved_tokens: Directional token estimate avoided versus broad
            file reads.
        hits: Ranked hits from the lookup phase.
        snippets: Bounded source snippets grouped by ranked files.
    """

    query: str
    repo_paths: list[str]
    hit_count: int
    candidate_files: int
    selected_files: int
    selected_lines: int
    estimated_tokens: int
    estimated_saved_tokens: int
    hits: list[ContextPackHit]
    snippets: list[ContextSnippet]


def build_context_pack(
    repo_path: str | Path,
    store: CatalogStore,
    query: str,
    *,
    limit: int = DEFAULT_CONTEXT_PACK_LIMIT,
    max_files: int = DEFAULT_CONTEXT_PACK_MAX_FILES,
    context_lines: int = DEFAULT_CONTEXT_PACK_LINES,
    max_lines_per_file: int = DEFAULT_CONTEXT_PACK_MAX_LINES_PER_FILE,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    fallback_text_limit: int | None = None,
    include_tests: bool = True,
) -> ContextPack:
    """Build a compact context pack for one repository.

    Args:
        repo_path: Repository root to search.
        store: Catalog store for the repository.
        query: Lookup query.
        limit: Maximum ranked hits to consider.
        max_files: Maximum distinct files to include snippets for.
        context_lines: Lines before and after text hits.
        max_lines_per_file: Maximum lines returned per selected file.
        symbol_limit: Optional symbol lookup limit.
        file_limit: Optional file path lookup limit.
        text_limit: Optional text lookup limit.
        fallback_text_limit: Optional text hit budget used only when text lookup
            is disabled and source/file lookup finds no hits.
        include_tests: Whether test files may be selected for snippets.

    Returns:
        Context pack with ranked hits and bounded snippets.

    Raises:
        ValueError: If the query is empty or no files can be selected.
        OSError: If a selected source file cannot be read.
    """
    repo_root = Path(repo_path).expanduser().resolve()
    lookup_limit = _lookup_limit_for_context(limit=limit, max_files=max_files, include_tests=include_tests)
    result = lookup(
        repo_root,
        store,
        query,
        limit=lookup_limit,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=text_limit,
        fallback_text_limit=fallback_text_limit,
        context_lines=0,
        include_tests=include_tests,
    )
    repo_label = _repo_label(repo_root)
    hits = [
        ContextPackHit(
            repo_path=str(repo_root),
            repo_label=repo_label,
            kind=hit.kind,
            path=hit.path,
            line=hit.line,
            label=hit.label,
            score=hit.score,
            end_line=hit.end_line,
        )
        for hit in result.hits
    ]
    hits = _context_hits(hits, limit=limit, include_tests=include_tests)
    snippets = _snippets_for_hits(
        hits,
        stores_by_repo={str(repo_root): store},
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        include_tests=include_tests,
    )
    return _context_pack(
        query=result.query,
        repo_paths=[str(repo_root)],
        stores_by_repo={str(repo_root): store},
        hits=hits,
        snippets=snippets,
    )


def build_workspace_context_pack(
    repo_paths: list[str | Path],
    query: str,
    *,
    limit: int = DEFAULT_CONTEXT_PACK_LIMIT,
    per_repo_limit: int | None = None,
    max_files: int = DEFAULT_CONTEXT_PACK_MAX_FILES,
    context_lines: int = DEFAULT_CONTEXT_PACK_LINES,
    max_lines_per_file: int = DEFAULT_CONTEXT_PACK_MAX_LINES_PER_FILE,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    fallback_text_limit: int | None = None,
    include_tests: bool = True,
    stores_by_repo: dict[str, CatalogStore] | None = None,
) -> ContextPack:
    """Build a compact context pack across several repositories.

    Args:
        repo_paths: Repository roots to search.
        query: Lookup query.
        limit: Maximum combined hits to consider.
        per_repo_limit: Maximum hits requested from each repository.
        max_files: Maximum distinct files to include snippets for.
        context_lines: Lines before and after text hits.
        max_lines_per_file: Maximum lines returned per selected file.
        symbol_limit: Optional symbol lookup limit per repository.
        file_limit: Optional file path lookup limit per repository.
        text_limit: Optional text lookup limit per repository.
        fallback_text_limit: Optional per-repository text hit budget used only
            when text lookup is disabled and source/file lookup finds no hits.
        include_tests: Whether test files may be selected for snippets.
        stores_by_repo: Optional reusable catalog stores keyed by absolute
            repository path. Supplying stores lets repeated workflow runs avoid
            opening the same SQLite catalog several times.

    Returns:
        Context pack with repo-qualified hits and bounded snippets.

    Raises:
        ValueError: If no repositories or query are supplied.
        OSError: If a selected source file cannot be read.
    """
    repo_roots = [_resolve_repo_root(repo_path) for repo_path in repo_paths]
    resolved_stores_by_repo = _stores_for_repos(repo_roots, stores_by_repo=stores_by_repo)
    lookup_limit = _lookup_limit_for_context(limit=limit, max_files=max_files, include_tests=include_tests)
    result = workspace_lookup(
        repo_roots,
        query,
        limit=lookup_limit,
        per_repo_limit=per_repo_limit,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=text_limit,
        fallback_text_limit=fallback_text_limit,
        context_lines=0,
        include_tests=include_tests,
        stores_by_repo=resolved_stores_by_repo,
    )
    hits = [
        ContextPackHit(
            repo_path=hit.repo_path,
            repo_label=hit.repo_label,
            kind=hit.kind,
            path=hit.path,
            line=hit.line,
            label=hit.label,
            score=hit.score,
            end_line=hit.end_line,
        )
        for hit in result.hits
    ]
    hits = _context_hits(hits, limit=limit, include_tests=include_tests)
    snippets = _snippets_for_hits(
        hits,
        stores_by_repo=resolved_stores_by_repo,
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        include_tests=include_tests,
    )
    return _context_pack(
        query=result.query,
        repo_paths=[str(repo_root) for repo_root in repo_roots],
        stores_by_repo=resolved_stores_by_repo,
        hits=hits,
        snippets=snippets,
    )


def context_pack_to_dict(pack: ContextPack, *, compact: bool = False, include_hits: bool = True) -> dict[str, Any]:
    """Serialize a context pack.

    Args:
        pack: Context pack to serialize.
        compact: When true, omit repeated nested repository paths, labels, and
            redundant ranking metadata; nested rows include repo_index into the
            top-level repo_paths and repo_labels lists instead.
        include_hits: Whether to include ranked lookup hit metadata. Snippets and
            ``hit_count`` remain available when hit rows are omitted.

    Returns:
        JSON-serializable context pack payload.
    """
    repo_paths, repo_labels, repo_index_by_path = _serialized_repos(pack)
    payload: dict[str, Any] = {
        "query": pack.query,
        "repo_paths": repo_paths,
        "hit_count": pack.hit_count,
        "candidate_files": pack.candidate_files,
        "selected_files": pack.selected_files,
        "selected_lines": pack.selected_lines,
        "estimated_tokens": pack.estimated_tokens,
        "estimated_saved_tokens": pack.estimated_saved_tokens,
        "snippets": [
            _context_snippet_to_dict(snippet, repo_index_by_path=repo_index_by_path, compact=compact)
            for snippet in pack.snippets
        ],
    }
    if include_hits:
        payload["hits"] = [
            _context_hit_to_dict(hit, repo_index_by_path=repo_index_by_path, compact=compact) for hit in pack.hits
        ]
    if compact:
        payload["repo_labels"] = repo_labels
    return payload


def _serialized_repos(pack: ContextPack) -> tuple[list[str], list[str], dict[str, int]]:
    repo_paths = list(pack.repo_paths)
    repo_index_by_path = {repo_path: index for index, repo_path in enumerate(repo_paths)}
    repo_labels_by_path: dict[str, str] = {hit.repo_path: hit.repo_label for hit in pack.hits if hit.repo_label} | {
        snippet.repo_path: snippet.repo_label for snippet in pack.snippets if snippet.repo_label
    }
    for repo_path in [*(hit.repo_path for hit in pack.hits), *(snippet.repo_path for snippet in pack.snippets)]:
        if repo_path not in repo_index_by_path:
            repo_index_by_path[repo_path] = len(repo_paths)
            repo_paths.append(repo_path)
    repo_labels = [repo_labels_by_path.get(repo_path) or _repo_label(Path(repo_path)) for repo_path in repo_paths]
    return repo_paths, repo_labels, repo_index_by_path


def _stores_for_repos(
    repo_roots: list[Path],
    *,
    stores_by_repo: dict[str, CatalogStore] | None,
) -> dict[str, CatalogStore]:
    supplied = stores_by_repo or {}
    return {
        str(repo_root): supplied.get(str(repo_root)) or CatalogStore.for_repo(repo_root, reuse_connection=True)
        for repo_root in repo_roots
    }


def _resolve_repo_root(repo_path: str | Path) -> Path:
    if isinstance(repo_path, Path) and repo_path.is_absolute():
        return repo_path
    return Path(repo_path).expanduser().resolve()


def _context_hit_to_dict(
    hit: ContextPackHit,
    *,
    repo_index_by_path: dict[str, int],
    compact: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": hit.kind,
        "path": hit.path,
        "line": hit.line,
        "label": hit.label,
    }
    if compact:
        row["repo_index"] = repo_index_by_path[hit.repo_path]
    else:
        row["score"] = hit.score
        row["repo_path"] = hit.repo_path
        row["repo_label"] = hit.repo_label
    return row


def _context_snippet_to_dict(
    snippet: ContextSnippet,
    *,
    repo_index_by_path: dict[str, int],
    compact: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": snippet.path,
        "start_line": snippet.start_line,
        "end_line": snippet.end_line,
        "hit_lines": snippet.hit_lines,
        "content": snippet.content,
    }
    if compact:
        row["repo_index"] = repo_index_by_path[snippet.repo_path]
    else:
        row["line_count"] = snippet.line_count
        row["repo_path"] = snippet.repo_path
        row["repo_label"] = snippet.repo_label
    return row


def context_pack_selected_paths(pack: ContextPack) -> dict[str, set[str]]:
    """Return selected source paths grouped by repository.

    Args:
        pack: Context pack.

    Returns:
        Mapping from absolute repository path to cataloged paths.
    """
    return _selected_paths_by_repo(pack.snippets)


def context_pack_snippet_tokens_by_repo(pack: ContextPack) -> dict[str, int]:
    """Return snippet token estimates grouped by repository.

    Args:
        pack: Context pack.

    Returns:
        Mapping from absolute repository path to estimated returned tokens.
    """
    return _snippet_tokens_by_repo(pack.snippets)


def context_pack_hit_counts_by_repo(pack: ContextPack) -> dict[str, int]:
    """Return ranked hit counts grouped by repository.

    Args:
        pack: Context pack.

    Returns:
        Mapping from absolute repository path to returned ranked hit counts.
    """
    return _hit_counts_by_repo(pack.hits)


def _context_pack(
    *,
    query: str,
    repo_paths: list[str],
    stores_by_repo: dict[str, CatalogStore],
    hits: list[ContextPackHit],
    snippets: list[ContextSnippet],
) -> ContextPack:
    selected_files = {(snippet.repo_path, snippet.path) for snippet in snippets}
    selected_lines = sum(max(0, snippet.end_line - snippet.start_line + 1) for snippet in snippets)
    estimated_tokens = _estimate_tokens("\n".join(snippet.content for snippet in snippets))
    selected_paths_by_repo = _selected_paths_by_repo(snippets)
    token_counts_by_repo = _snippet_tokens_by_repo(snippets)
    hit_counts_by_repo = _hit_counts_by_repo(hits)

    candidate_files = 0
    estimated_saved_tokens = 0
    for repo_path in repo_paths:
        store = stores_by_repo.get(repo_path)
        if store is None:
            continue
        metrics = estimate_saved_tokens_for_context_pack(
            store,
            selected_paths_by_repo.get(repo_path, set()),
            token_counts_by_repo.get(repo_path, 0),
            hit_counts_by_repo.get(repo_path, 0),
        )
        candidate_files += int(metrics["candidate_files"])
        estimated_saved_tokens += int(metrics["estimated_saved_tokens"])
    return ContextPack(
        query=query,
        repo_paths=repo_paths,
        hit_count=len(hits),
        candidate_files=candidate_files,
        selected_files=len(selected_files),
        selected_lines=selected_lines,
        estimated_tokens=estimated_tokens,
        estimated_saved_tokens=estimated_saved_tokens,
        hits=hits,
        snippets=snippets,
    )


def _selected_paths_by_repo(snippets: list[ContextSnippet]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for snippet in snippets:
        grouped.setdefault(snippet.repo_path, set()).add(snippet.path)
    return grouped


def _snippet_tokens_by_repo(snippets: list[ContextSnippet]) -> dict[str, int]:
    content_by_repo: dict[str, list[str]] = {}
    for snippet in snippets:
        content_by_repo.setdefault(snippet.repo_path, []).append(snippet.content)
    return {repo_path: _estimate_tokens("\n".join(contents)) for repo_path, contents in content_by_repo.items()}


def _hit_counts_by_repo(hits: list[ContextPackHit]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.repo_path] = counts.get(hit.repo_path, 0) + 1
    return counts


def _lookup_limit_for_context(*, limit: int, max_files: int, include_tests: bool) -> int:
    bounded_limit = max(1, min(limit, 100))
    if include_tests:
        return bounded_limit
    bounded_max_files = max(1, min(max_files, 20))
    return max(bounded_limit, min(100, bounded_limit + bounded_max_files * 5))


def _context_hits(hits: list[ContextPackHit], *, limit: int, include_tests: bool) -> list[ContextPackHit]:
    bounded_limit = max(1, min(limit, 100))
    if include_tests:
        return hits[:bounded_limit]
    return [hit for hit in hits if not _is_test_path(hit.path)][:bounded_limit]


def _snippets_for_hits(
    hits: list[ContextPackHit],
    *,
    stores_by_repo: dict[str, CatalogStore],
    max_files: int,
    context_lines: int,
    max_lines_per_file: int,
    include_tests: bool,
) -> list[ContextSnippet]:
    bounded_max_files = max(1, min(max_files, 20))
    bounded_context = max(0, min(context_lines, 20))
    bounded_max_lines = max(1, min(max_lines_per_file, 500))
    hits_by_file: dict[tuple[str, str], list[ContextPackHit]] = {}

    for hit in hits:
        if not hit.path:
            continue
        if not include_tests and _is_test_path(hit.path):
            continue
        key = (hit.repo_path, hit.path)
        if key not in hits_by_file and len(hits_by_file) >= bounded_max_files:
            continue
        hits_by_file.setdefault(key, []).append(hit)

    snippets: list[ContextSnippet] = []
    for (repo_path, _path), file_hits in hits_by_file.items():
        store = stores_by_repo[repo_path]
        snippets.extend(
            _snippets_for_file(
                repo_path=Path(repo_path),
                store=store,
                file_hits=file_hits,
                context_lines=bounded_context,
                max_lines_per_file=bounded_max_lines,
            )
        )
    return snippets


def _snippets_for_file(
    *,
    repo_path: Path,
    store: CatalogStore,
    file_hits: list[ContextPackHit],
    context_lines: int,
    max_lines_per_file: int,
) -> list[ContextSnippet]:
    path = file_hits[0].path
    source_path = repo_path / path
    file_row = store.get_file(path)
    catalog_line_count = int(file_row["line_count"]) if file_row is not None else 0
    use_catalog_lines = _should_use_catalog_lines(store, source_path, file_row)
    lines: list[str] = []
    if use_catalog_lines:
        line_count = catalog_line_count
    else:
        lines = redact_source_text(source_path.read_text(errors="replace")).splitlines()
        line_count = len(lines)
    if line_count == 0:
        return []
    windows = _bounded_windows(
        [
            window
            for hit in file_hits
            for window in _windows_for_hit(
                store,
                hit,
                line_count=line_count,
                context_lines=context_lines,
                max_lines_per_file=max_lines_per_file,
            )
        ],
        max_lines=max_lines_per_file,
    )
    snippets: list[ContextSnippet] = []
    for start_line, end_line in windows:
        content = (
            _catalog_window_content(store, path, start_line=start_line, end_line=end_line)
            if use_catalog_lines
            else "\n".join(lines[start_line - 1 : end_line])
        )
        snippets.append(
            ContextSnippet(
                repo_path=str(repo_path),
                repo_label=file_hits[0].repo_label,
                path=path,
                start_line=start_line,
                end_line=end_line,
                line_count=line_count,
                hit_lines=[hit.line for hit in file_hits if start_line <= hit.line <= end_line],
                content=content,
            )
        )
    return snippets


def _should_use_catalog_lines(store: CatalogStore, source_path: Path, file_row: Any) -> bool:
    if file_row is None:
        return False
    if not store.supports_text_index():
        return False
    size_bytes = int(file_row["size_bytes"] or 0)
    modified_ns = int(file_row["modified_ns"] or 0)
    if modified_ns <= 0:
        return False
    try:
        stat_result = source_path.stat()
    except OSError:
        return False
    return stat_result.st_mtime_ns == modified_ns and stat_result.st_size == size_bytes


def _catalog_window_content(store: CatalogStore, path: str, *, start_line: int, end_line: int) -> str:
    indexed_lines = store.source_line_range(path, start_line, end_line)
    return "\n".join(indexed_lines.get(line_number, "") for line_number in range(start_line, end_line + 1))


def _windows_for_hit(
    store: CatalogStore,
    hit: ContextPackHit,
    *,
    line_count: int,
    context_lines: int,
    max_lines_per_file: int,
) -> list[tuple[int, int]]:
    if hit.kind == "file":
        symbol_windows = _symbol_windows_for_file_hit(
            store,
            hit,
            line_count=line_count,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
        )
        return symbol_windows or [(1, min(line_count, max_lines_per_file))]

    start_line = max(1, hit.line - context_lines)
    end_line = min(line_count, hit.line + context_lines)
    if hit.kind == "symbol":
        start_line = max(1, hit.line - min(context_lines, 2))
        if hit.end_line is not None:
            end_line = min(line_count, max(hit.end_line, hit.line), start_line + max_lines_per_file - 1)
        else:
            symbol_row = _symbol_at_line(store, hit.path, hit.line)
            if symbol_row is not None:
                raw_end_line = symbol_row["end_line"]
                symbol_end_line = int(raw_end_line) if raw_end_line is not None else hit.line + context_lines
                end_line = min(line_count, max(symbol_end_line, hit.line), start_line + max_lines_per_file - 1)
    return [(start_line, max(start_line, end_line))]


def _symbol_windows_for_file_hit(
    store: CatalogStore,
    hit: ContextPackHit,
    *,
    line_count: int,
    context_lines: int,
    max_lines_per_file: int,
) -> list[tuple[int, int]]:
    symbol_rows = [dict(row) for row in store.symbols_for_file(hit.path)]
    if not symbol_rows:
        return []

    preferred_rows = _preferred_file_symbols(symbol_rows)
    windows: list[tuple[int, int]] = []
    for row in preferred_rows:
        line = int(row["line"])
        raw_end_line = row["end_line"]
        start_line = line
        end_line = min(line_count, line + context_lines)
        if raw_end_line is not None:
            end_line = min(line_count, max(int(raw_end_line), line), start_line + max_lines_per_file - 1)
        windows.append((start_line, max(start_line, end_line)))
    return windows


def _preferred_file_symbols(symbol_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    explicit_exports = [row for row in symbol_rows if _is_explicit_export(row)]
    catalog_exports = [row for row in symbol_rows if bool(row.get("exported", True))]
    candidates = explicit_exports or catalog_exports or symbol_rows
    public_kinds = {"class", "function", "method", "component", "hook", "type"}
    public_rows = [row for row in candidates if str(row.get("kind", "")) in public_kinds]
    constant_rows = [row for row in candidates if str(row.get("kind", "")) == "constant"]
    remaining_rows = [row for row in candidates if row not in public_rows and row not in constant_rows]
    return sorted([*constant_rows[:4], *public_rows, *remaining_rows], key=lambda row: int(row["line"]))


def _is_explicit_export(row: dict[str, Any]) -> bool:
    signature = str(row.get("signature", "")).lstrip()
    return signature.startswith("export ")


def _bounded_windows(windows: list[tuple[int, int]], *, max_lines: int) -> list[tuple[int, int]]:
    if not windows:
        return []
    merged: list[tuple[int, int]] = []
    for start_line, end_line in sorted(windows):
        if not merged or start_line > merged[-1][1] + 1:
            merged.append((start_line, end_line))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end_line))

    bounded: list[tuple[int, int]] = []
    remaining = max_lines
    for start_line, end_line in merged:
        if remaining <= 0:
            break
        bounded_end = min(end_line, start_line + remaining - 1)
        bounded.append((start_line, bounded_end))
        remaining -= bounded_end - start_line + 1
    return bounded


def _symbol_at_line(store: CatalogStore, path: str, line: int) -> dict[str, Any] | None:
    for row in store.symbols_for_file(path):
        if int(row["line"]) == line:
            return dict(row)
    return None


def _repo_label(repo_path: Path) -> str:
    parent = repo_path.parent.name
    name = repo_path.name
    if name == "src" and parent:
        return f"{parent}/{name}"
    return name


def _is_test_path(path: str) -> bool:
    parsed = Path(path)
    parts = set(parsed.parts)
    name = parsed.name
    return (
        bool(parts & {"test", "tests", "__tests__"})
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def _estimate_tokens(content: str) -> int:
    return max(0, (len(content) + 3) // 4)
