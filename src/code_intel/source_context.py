"""Precise source-context retrieval from a code-intel catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_intel.catalog_store import CatalogStore


def get_file_tree(
    repo_path: str | Path,
    store: CatalogStore,
    *,
    prefix: str = "",
    max_depth: int = 3,
    max_entries: int = 200,
) -> dict[str, Any]:
    """Return a compact catalog-backed file tree.

    Args:
        repo_path: Repository path used for response metadata.
        store: Catalog store for the repository.
        prefix: Optional repository-relative directory prefix to inspect.
        max_depth: Maximum path depth relative to the prefix.
        max_entries: Maximum tree entries to return.

    Returns:
        Dictionary with directory and file entries including file, line, and
        symbol counts.
    """
    repo_root = Path(repo_path).resolve()
    normalized_prefix = _normalize_prefix(prefix)
    bounded_depth = max(1, min(max_depth, 10))
    bounded_entries = max(1, min(max_entries, 1000))
    files = _filtered_file_rows(store, normalized_prefix)
    symbol_counts = store.symbol_counts_by_file()
    entries = _tree_entries(files, symbol_counts, prefix=normalized_prefix, max_depth=bounded_depth)
    returned_entries = entries[:bounded_entries]
    return {
        "repo_path": str(repo_root),
        "prefix": normalized_prefix,
        "max_depth": bounded_depth,
        "total_files": len(files),
        "returned_entries": len(returned_entries),
        "truncated": len(entries) > len(returned_entries),
        "entries": returned_entries,
    }


def get_repo_outline(
    repo_path: str | Path,
    store: CatalogStore,
    *,
    max_depth: int = 2,
    top_files: int = 20,
) -> dict[str, Any]:
    """Return a compact repository outline from the catalog.

    Args:
        repo_path: Repository path used for response metadata.
        store: Catalog store for the repository.
        max_depth: Directory depth to aggregate.
        top_files: Maximum symbol-heavy files to include.

    Returns:
        Dictionary with language totals, directory summaries, and top files.
    """
    repo_root = Path(repo_path).resolve()
    bounded_depth = max(1, min(max_depth, 10))
    bounded_top_files = max(1, min(top_files, 100))
    files = [dict(row) for row in store.list_files()]
    symbol_counts = store.symbol_counts_by_file()
    total_lines = sum(int(row["line_count"]) for row in files)
    total_symbols = sum(symbol_counts.values())
    return {
        "repo_path": str(repo_root),
        "total_files": len(files),
        "total_lines": total_lines,
        "total_symbols": total_symbols,
        "languages": _language_summaries(files),
        "directories": _directory_summaries(files, symbol_counts, max_depth=bounded_depth),
        "top_files": _top_symbol_files(files, symbol_counts, limit=bounded_top_files),
    }


def get_workspace_outline(
    repo_paths: list[str | Path],
    *,
    max_depth: int = 2,
    top_files: int = 10,
    stores_by_repo: dict[str, CatalogStore] | None = None,
) -> dict[str, Any]:
    """Return a compact multi-repository outline from warm catalogs.

    Args:
        repo_paths: Repository roots to inspect.
        max_depth: Directory depth to aggregate per repository.
        top_files: Maximum symbol-heavy files to include per repository and in
            the workspace-wide top-file list.
        stores_by_repo: Optional pre-opened catalog stores keyed by absolute
            repository path. Used by MCP flows to avoid repeated SQLite
            connection setup.

    Returns:
        Dictionary with aggregate totals, language totals, per-repository
        summaries, and workspace-ranked top files.

    Raises:
        ValueError: If no repository path is supplied.
    """
    if not repo_paths:
        raise ValueError("at least one repository path is required")
    bounded_depth = max(1, min(max_depth, 10))
    bounded_top_files = max(1, min(top_files, 100))
    repos: list[dict[str, Any]] = []
    top_file_candidates: list[dict[str, Any]] = []
    aggregate_languages: dict[str, dict[str, Any]] = {}

    for raw_repo_path in repo_paths:
        repo_root = Path(raw_repo_path).expanduser().resolve()
        repo_label = _repo_label(repo_root)
        store = stores_by_repo.get(str(repo_root)) if stores_by_repo else CatalogStore.for_repo(repo_root)
        if not store.has_catalog():
            repos.append(
                {
                    "repo_path": str(repo_root),
                    "label": repo_label,
                    "searched": False,
                    "error": f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first.",
                }
            )
            continue

        outline = get_repo_outline(repo_root, store, max_depth=bounded_depth, top_files=bounded_top_files)
        for language in outline["languages"]:
            aggregate = aggregate_languages.setdefault(
                str(language["language"]),
                {"language": str(language["language"]), "file_count": 0, "line_count": 0},
            )
            aggregate["file_count"] += int(language["file_count"])
            aggregate["line_count"] += int(language["line_count"])
        enriched_top_files = [
            _repo_scoped_file(row, repo_root=repo_root, repo_label=repo_label) for row in outline["top_files"]
        ]
        top_file_candidates.extend(enriched_top_files)
        repos.append(
            {
                "repo_path": str(repo_root),
                "label": repo_label,
                "searched": True,
                "error": "",
                "total_files": int(outline["total_files"]),
                "total_lines": int(outline["total_lines"]),
                "total_symbols": int(outline["total_symbols"]),
                "languages": outline["languages"],
                "top_files": enriched_top_files,
            }
        )

    searched_repos = [repo for repo in repos if repo["searched"]]
    return {
        "repo_count": len(repos),
        "searched_repos": len(searched_repos),
        "total_files": sum(int(repo.get("total_files", 0)) for repo in searched_repos),
        "total_lines": sum(int(repo.get("total_lines", 0)) for repo in searched_repos),
        "total_symbols": sum(int(repo.get("total_symbols", 0)) for repo in searched_repos),
        "languages": sorted(
            aggregate_languages.values(),
            key=lambda summary: (-int(summary["file_count"]), str(summary["language"])),
        ),
        "repos": repos,
        "top_files": _rank_workspace_top_files(top_file_candidates, limit=bounded_top_files),
    }


def get_file_outline(repo_path: str | Path, store: CatalogStore, file_path: str) -> dict[str, Any]:
    """Return cataloged symbols for one file.

    Args:
        repo_path: Repository path used for response metadata.
        store: Catalog store for the repository.
        file_path: Repository-relative path or unique suffix for a cataloged file.

    Returns:
        Dictionary with file metadata and ordered symbols.

    Raises:
        ValueError: If the file is not cataloged.
    """
    repo_root = Path(repo_path).resolve()
    resolved_path = _resolve_cataloged_file(store, file_path)
    file_row = store.get_file(resolved_path)
    symbols = store.symbols_for_file(resolved_path)
    return {
        "repo_path": str(repo_root),
        "path": resolved_path,
        "language": str(file_row["language"]) if file_row else "",
        "line_count": int(file_row["line_count"]) if file_row else 0,
        "size_bytes": int(file_row["size_bytes"]) if file_row else 0,
        "symbol_count": len(symbols),
        "symbols": [
            {
                "name": str(row["name"]),
                "qualified_name": str(row["qualified_name"]),
                "kind": str(row["kind"]),
                "line": int(row["line"]),
                "end_line": int(row["end_line"]) if row["end_line"] is not None else None,
                "signature": str(row["signature"]),
                "summary": str(row["doc"]),
                "exported": bool(row["exported"]),
            }
            for row in symbols
        ],
    }


def get_file_content(
    repo_path: str | Path,
    store: CatalogStore,
    file_path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Return bounded source content for a cataloged file.

    Args:
        repo_path: Repository path containing the cataloged file.
        store: Catalog store for the repository.
        file_path: Repository-relative path or unique suffix for a cataloged file.
        start_line: Optional one-based first line to include.
        end_line: Optional one-based final line to include.

    Returns:
        Dictionary with source content and line-range metadata.

    Raises:
        ValueError: If the file is not cataloged or the requested line range is invalid.
        OSError: If the cataloged source file cannot be read.
    """
    repo_root = Path(repo_path).resolve()
    resolved_path = _resolve_cataloged_file(store, file_path)
    source_path = repo_root / resolved_path
    source = source_path.read_text(errors="replace")
    lines = source.splitlines()
    total_lines = len(lines)

    first = start_line if start_line is not None else 1
    last = end_line if end_line is not None else total_lines
    if first < 1:
        raise ValueError("start_line must be >= 1")
    if last < first:
        raise ValueError("end_line must be >= start_line")

    bounded_first = min(first, total_lines + 1)
    bounded_last = min(last, total_lines)
    selected = lines[bounded_first - 1 : bounded_last] if bounded_first <= bounded_last else []
    return {
        "repo_path": str(repo_root),
        "path": resolved_path,
        "start_line": bounded_first,
        "end_line": bounded_last,
        "line_count": total_lines,
        "content": "\n".join(selected),
    }


def _resolve_cataloged_file(store: CatalogStore, file_path: str) -> str:
    resolved_path = store.resolve_file_path(file_path)
    if resolved_path is None:
        raise ValueError(f"File is not cataloged: {file_path}")
    return resolved_path


def _normalize_prefix(prefix: str) -> str:
    normalized = Path(prefix).as_posix().strip("/")
    if normalized == ".":
        return ""
    return normalized


def _filtered_file_rows(store: CatalogStore, prefix: str) -> list[dict[str, Any]]:
    files = [dict(row) for row in store.list_files()]
    if not prefix:
        return files
    prefix_with_sep = f"{prefix}/"
    return [row for row in files if str(row["path"]) == prefix or str(row["path"]).startswith(prefix_with_sep)]


def _tree_entries(
    files: list[dict[str, Any]],
    symbol_counts: dict[str, int],
    *,
    prefix: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    directory_stats: dict[str, dict[str, Any]] = {}
    file_entries: list[dict[str, Any]] = []
    prefix_depth = len(prefix.split("/")) if prefix else 0

    for row in files:
        path = str(row["path"])
        parts = path.split("/")
        relative_depth = len(parts) - prefix_depth
        for depth in range(1, min(relative_depth, max_depth) + 1):
            directory_path = "/".join(parts[: prefix_depth + depth])
            if directory_path == path:
                continue
            stats = directory_stats.setdefault(
                directory_path,
                {
                    "path": directory_path,
                    "type": "directory",
                    "depth": depth,
                    "file_count": 0,
                    "line_count": 0,
                    "symbol_count": 0,
                    "languages": set(),
                },
            )
            stats["file_count"] += 1
            stats["line_count"] += int(row["line_count"])
            stats["symbol_count"] += symbol_counts.get(path, 0)
            stats["languages"].add(str(row["language"]))

        if relative_depth <= max_depth:
            file_entries.append(_file_entry(row, symbol_counts.get(path, 0), depth=relative_depth))

    directory_entries = [_serialize_directory_entry(stats) for stats in directory_stats.values()]
    return sorted(
        [*directory_entries, *file_entries],
        key=lambda entry: (int(entry["depth"]), str(entry["path"]), str(entry["type"])),
    )


def _file_entry(row: dict[str, Any], symbol_count: int, *, depth: int) -> dict[str, Any]:
    return {
        "path": str(row["path"]),
        "type": "file",
        "depth": depth,
        "language": str(row["language"]),
        "line_count": int(row["line_count"]),
        "size_bytes": int(row["size_bytes"]),
        "symbol_count": symbol_count,
    }


def _serialize_directory_entry(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(stats["path"]),
        "type": "directory",
        "depth": int(stats["depth"]),
        "file_count": int(stats["file_count"]),
        "line_count": int(stats["line_count"]),
        "symbol_count": int(stats["symbol_count"]),
        "languages": sorted(stats["languages"]),
    }


def _language_summaries(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for row in files:
        language = str(row["language"])
        summary = summaries.setdefault(language, {"language": language, "file_count": 0, "line_count": 0})
        summary["file_count"] += 1
        summary["line_count"] += int(row["line_count"])
    return sorted(summaries.values(), key=lambda summary: (-int(summary["file_count"]), str(summary["language"])))


def _directory_summaries(
    files: list[dict[str, Any]],
    symbol_counts: dict[str, int],
    *,
    max_depth: int,
) -> list[dict[str, Any]]:
    entries = _tree_entries(files, symbol_counts, prefix="", max_depth=max_depth)
    return [entry for entry in entries if entry["type"] == "directory"]


def _top_symbol_files(
    files: list[dict[str, Any]],
    symbol_counts: dict[str, int],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    entries = [
        _file_entry(row, symbol_counts.get(str(row["path"]), 0), depth=len(str(row["path"]).split("/")))
        for row in files
    ]
    ranked = sorted(
        entries,
        key=lambda entry: (-int(entry["symbol_count"]), -int(entry["line_count"]), str(entry["path"])),
    )
    return ranked[:limit]


def _repo_scoped_file(row: dict[str, Any], *, repo_root: Path, repo_label: str) -> dict[str, Any]:
    scoped = dict(row)
    scoped["repo_path"] = str(repo_root)
    scoped["repo_label"] = repo_label
    return scoped


def _rank_workspace_top_files(files: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        files,
        key=lambda entry: (
            -int(entry["symbol_count"]),
            -int(entry["line_count"]),
            str(entry["repo_label"]),
            str(entry["path"]),
        ),
    )
    return ranked[:limit]


def _repo_label(repo_path: Path) -> str:
    parent = repo_path.parent.name
    name = repo_path.name
    if name == "src" and parent:
        return f"{parent}/{name}"
    return name
