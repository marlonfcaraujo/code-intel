"""Catalog freshness and repository-state checks."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from code_intel.analyzers import SUPPORTED_EXTENSIONS
from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import CATALOG_ANALYZER_VERSION
from code_intel.discovery import discover_source_files

MAX_REPORTED_PATHS = 20
GIT_COMMAND_TIMEOUT_SECONDS = 10


def assess_catalog_health(
    repo_path: str | Path,
    store: CatalogStore | None = None,
    *,
    summary_only: bool = False,
) -> dict[str, Any]:
    """Assess whether a catalog is safe to trust for agent navigation.

    Args:
        repo_path: Repository path whose catalog should be checked.
        store: Optional preconfigured catalog store.
        summary_only: When true, return a compact health payload for agents and
            schedulers that only need trust/freshness counts.

    Returns:
        Dictionary containing catalog existence, counts, metadata, and a
        freshness verdict with concrete stale reasons.
    """
    repo_root = Path(repo_path).resolve()
    catalog_store = store or CatalogStore.for_repo(repo_root)
    result: dict[str, Any] = {
        "repo_path": str(repo_root),
        "repo_exists": repo_root.exists(),
        "git_repo": bool(_git_top_level(repo_root)),
        "catalog_path": str(catalog_store.database_path),
        "database_exists": catalog_store.database_path.exists(),
        "catalog_exists": catalog_store.has_catalog(),
    }
    if not catalog_store.has_catalog():
        result["freshness"] = {
            "status": "missing",
            "stale": True,
            "reasons": ["catalog_missing"],
        }
        return _summarize_health(result) if summary_only else result

    meta = catalog_store.get_meta()
    files = catalog_store.list_files()
    freshness = _assess_freshness(repo_root, catalog_store, meta, files)
    if summary_only:
        result.update(
            {
                "meta": _summarize_meta(meta),
                "files": catalog_store.file_count(),
                "symbols": catalog_store.symbol_count(),
                "dependencies": catalog_store.dependency_count(),
                "text_lines": catalog_store.text_line_count(),
                "freshness": _summarize_freshness(freshness),
            }
        )
        if catalog_store.database_path.exists():
            result["usage"] = catalog_store.usage_summary(detailed=False)
        return result

    result.update(
        {
            "meta": meta,
            "files": catalog_store.file_count(),
            "symbols": catalog_store.symbol_count(),
            "dependencies": catalog_store.dependency_count(),
            "text_lines": catalog_store.text_line_count(),
            "dependency_summary": catalog_store.dependency_summary(limit=MAX_REPORTED_PATHS),
            "freshness": freshness,
        }
    )
    if catalog_store.database_path.exists():
        result["usage"] = catalog_store.usage_summary()
    return result


def _summarize_health(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo_path": health.get("repo_path", ""),
        "repo_exists": bool(health.get("repo_exists", False)),
        "git_repo": bool(health.get("git_repo", False)),
        "catalog_path": health.get("catalog_path", ""),
        "database_exists": bool(health.get("database_exists", False)),
        "catalog_exists": bool(health.get("catalog_exists", False)),
        "freshness": _summarize_freshness(health.get("freshness", {})),
    }


def _summarize_meta(meta: dict[str, str]) -> dict[str, str]:
    keys = (
        "generated_at",
        "scan_mode",
        "analyzer_version",
        "file_count",
        "reused_file_count",
        "changed_file_count",
        "removed_file_count",
        "analysis_workers",
    )
    return {key: meta[key] for key in keys if key in meta}


def _summarize_freshness(freshness: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "stale",
        "reasons",
        "supports_incremental",
        "supports_text_index",
        "git_head_matches",
        "analyzer_version_matches",
        "git_dirty",
        "git_dirty_count",
        "cataloged_file_count",
        "discovered_file_count",
        "added_count",
        "removed_count",
        "changed_count",
    )
    return {key: freshness[key] for key in keys if key in freshness}


def _assess_freshness(
    repo_root: Path,
    store: CatalogStore,
    meta: dict[str, str],
    files: list[Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    current_git_head = _git_head(repo_root)
    catalog_git_head = meta.get("git_head", "")
    git_head_matches = bool(catalog_git_head and current_git_head and catalog_git_head == current_git_head)
    if catalog_git_head and current_git_head and not git_head_matches:
        reasons.append("git_head_changed")

    catalog_analyzer_version = meta.get("analyzer_version", "")
    analyzer_version_matches = catalog_analyzer_version == CATALOG_ANALYZER_VERSION
    if not analyzer_version_matches:
        reasons.append("catalog_analyzer_version_changed")

    git_dirty_paths = _git_dirty_paths(repo_root)
    if git_dirty_paths:
        reasons.append("git_worktree_dirty")

    discovered_paths = _discover_paths(repo_root)
    catalog_paths = {str(row["path"]) for row in files}
    added_paths = sorted(discovered_paths - catalog_paths)
    removed_paths = sorted(catalog_paths - discovered_paths)
    if added_paths:
        reasons.append("source_files_added")
    if removed_paths:
        reasons.append("source_files_removed")

    supports_incremental = store.supports_incremental_catalog()
    changed_paths = _changed_cataloged_paths(repo_root, files) if supports_incremental else []
    if changed_paths:
        reasons.append("cataloged_files_changed")

    if not supports_incremental:
        reasons.append("catalog_schema_lacks_fingerprints")
    supports_text_index = store.supports_text_index()
    if not supports_text_index:
        reasons.append("catalog_schema_lacks_text_index")

    stale = bool(reasons)
    return {
        "status": "stale" if stale else "fresh",
        "stale": stale,
        "reasons": reasons,
        "supports_incremental": supports_incremental,
        "supports_text_index": supports_text_index,
        "catalog_git_head": catalog_git_head,
        "current_git_head": current_git_head,
        "git_head_matches": git_head_matches,
        "catalog_analyzer_version": catalog_analyzer_version,
        "current_analyzer_version": CATALOG_ANALYZER_VERSION,
        "analyzer_version_matches": analyzer_version_matches,
        "git_dirty": bool(git_dirty_paths),
        "git_dirty_count": len(git_dirty_paths),
        "git_dirty_paths": git_dirty_paths[:MAX_REPORTED_PATHS],
        "cataloged_file_count": len(catalog_paths),
        "discovered_file_count": len(discovered_paths),
        "added_count": len(added_paths),
        "added_paths": added_paths[:MAX_REPORTED_PATHS],
        "removed_count": len(removed_paths),
        "removed_paths": removed_paths[:MAX_REPORTED_PATHS],
        "changed_count": len(changed_paths),
        "changed_paths": changed_paths[:MAX_REPORTED_PATHS],
    }


def _discover_paths(repo_root: Path) -> set[str]:
    if not repo_root.exists() or not repo_root.is_dir():
        return set()
    return {path.relative_to(repo_root).as_posix() for path in discover_source_files(repo_root, SUPPORTED_EXTENSIONS)}


def _changed_cataloged_paths(repo_root: Path, files: list[Any]) -> list[str]:
    changed: list[str] = []
    for row in files:
        rel_path = str(row["path"])
        path = repo_root / rel_path
        if not path.exists():
            continue
        stat = path.stat()
        if int(row["size_bytes"]) == stat.st_size and int(row["modified_ns"] or 0) == stat.st_mtime_ns:
            continue
        content_hash = str(row["content_hash"] or "")
        if not content_hash or _hash_file(path) != content_hash:
            changed.append(rel_path)
    return changed


def _git_head(repo_root: Path) -> str:
    result = _run_git(repo_root, "rev-parse", "HEAD")
    return result.strip()


def _git_top_level(repo_root: Path) -> str:
    result = _run_git(repo_root, "rev-parse", "--show-toplevel")
    return result.strip()


def _git_dirty_paths(repo_root: Path) -> list[str]:
    output = _run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        paths.append(line[3:].strip())
    return sorted(paths)


def _run_git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
