"""Repository catalog orchestration."""

from __future__ import annotations

import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from code_intel.analyzers import ANALYZERS, SUPPORTED_EXTENSIONS
from code_intel.catalog_store import CatalogStore
from code_intel.discovery import DiscoveredSourceFile, discover_source_file_entries
from code_intel.models import CatalogResult, FileAnalysis

CATALOG_ANALYZER_VERSION = "2026-07-02-secret-redaction"
GIT_COMMAND_TIMEOUT_SECONDS = 10
DEFAULT_ANALYSIS_WORKERS = min(32, max(1, os.cpu_count() or 1))
PARALLEL_ANALYSIS_MIN_FILES = 300
GIT_STATUS_FINGERPRINT_META_KEY = "git_status_fingerprint_v2"


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Filesystem fingerprint used for incremental scan decisions."""

    path: str
    size_bytes: int
    modified_ns: int
    content_hash: str


def build_catalog(
    repo_path: str | Path,
    database_path: str | Path | None = None,
    *,
    incremental: bool = False,
    workers: int | None = None,
    skip_unchanged_meta: bool = False,
) -> CatalogResult:
    """Build or refresh a repository catalog.

    Args:
        repo_path: Repository path to catalog.
        database_path: Optional explicit SQLite catalog path.
        incremental: When true, reuse unchanged file analyses from the existing
            catalog whenever source file paths are unchanged.
        workers: Optional number of analysis worker threads. Defaults to a
            bounded CPU-count value. Use 1 for deterministic serial profiling.
        skip_unchanged_meta: When true, skip the metadata-only write for
            incremental scans that detect no file changes. This is useful for
            frequent background refresh jobs where avoiding SQLite writes matters
            more than updating the catalog's ``generated_at`` timestamp.

    Returns:
        Summary of the completed catalog run.

    Raises:
        FileNotFoundError: If the repository path does not exist.
        NotADirectoryError: If the repository path is not a directory.
    """
    repo_root = Path(repo_path).resolve()
    if not repo_root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {repo_root}")

    total_started = perf_counter()
    store = CatalogStore.for_repo(repo_root, Path(database_path).resolve() if database_path else None)
    if incremental and store.supports_incremental_catalog() and store.supports_text_index():
        fast_path_result = _try_git_status_fast_path(
            store,
            repo_root,
            workers=workers,
            skip_unchanged_meta=skip_unchanged_meta,
            total_started=total_started,
        )
        if fast_path_result is not None:
            return fast_path_result

    discovery_started = perf_counter()
    source_entries = discover_source_file_entries(repo_root, SUPPORTED_EXTENSIONS)
    discovery_ms = _elapsed_ms(discovery_started)
    all_paths = {entry.rel_path for entry in source_entries}
    analysis_workers = _effective_workers(workers, file_count=len(source_entries))

    if incremental and store.supports_incremental_catalog() and store.supports_text_index():
        return _build_incremental_catalog(
            store,
            repo_root,
            source_entries,
            all_paths,
            workers=analysis_workers,
            skip_unchanged_meta=skip_unchanged_meta,
            discovery_ms=discovery_ms,
            total_started=total_started,
        )

    change_detection_started = perf_counter()
    fingerprints = {entry.rel_path: _fingerprint_from_entry(entry, hash_content=True) for entry in source_entries}
    change_detection_ms = _elapsed_ms(change_detection_started)
    analysis_started = perf_counter()
    analyses = _analyze_files(
        [(entry.path, fingerprints[entry.rel_path]) for entry in source_entries],
        repo_root,
        all_paths,
        workers=analysis_workers,
    )
    analysis_ms = _elapsed_ms(analysis_started)
    write_started = perf_counter()
    store.reset()
    store.write_catalog(
        analyses,
        _build_metadata(
            repo_root,
            analyses,
            incremental=False,
            reused_file_count=0,
            changed_file_count=len(analyses),
            removed_file_count=0,
            workers=analysis_workers,
        ),
    )
    write_ms = _elapsed_ms(write_started)

    return _catalog_result(
        store,
        repo_root=repo_root,
        reused_file_count=0,
        changed_file_count=len(analyses),
        removed_file_count=0,
        incremental=False,
        written_file_count=len(analyses),
        workers=analysis_workers,
        total_started=total_started,
        discovery_ms=discovery_ms,
        change_detection_ms=change_detection_ms,
        analysis_ms=analysis_ms,
        write_ms=write_ms,
    )


def _build_incremental_catalog(
    store: CatalogStore,
    repo_root: Path,
    source_entries: list[DiscoveredSourceFile],
    all_paths: set[str],
    *,
    workers: int,
    skip_unchanged_meta: bool,
    discovery_ms: float,
    total_started: float,
) -> CatalogResult:
    meta = store.get_meta()
    if meta.get("analyzer_version") != CATALOG_ANALYZER_VERSION:
        return build_catalog(repo_root, store.database_path, incremental=False, workers=workers)

    planning_started = perf_counter()
    old_files = {str(row["path"]): row for row in store.list_files()}
    old_paths = set(old_files)
    removed_paths = old_paths - all_paths
    added_paths = all_paths - old_paths
    source_entries_by_rel = {entry.rel_path: entry for entry in source_entries}
    dependency_reanalysis_paths = _dependency_reanalysis_paths(
        store,
        added_paths=added_paths,
        removed_paths=removed_paths,
        all_paths=all_paths,
    )
    planning_ms = _elapsed_ms(planning_started)

    change_detection_started = perf_counter()
    if not removed_paths and not added_paths and _all_file_stats_match(source_entries, old_files):
        change_detection_ms = _elapsed_ms(change_detection_started)
        reused_file_count = len(source_entries)
        write_started = perf_counter()
        should_seed_git_status = not meta.get(GIT_STATUS_FINGERPRINT_META_KEY) and _find_git_dir(repo_root) is not None
        if not skip_unchanged_meta or should_seed_git_status:
            store.update_meta(
                _build_metadata_for_count(
                    repo_root,
                    file_count=reused_file_count,
                    incremental=True,
                    reused_file_count=reused_file_count,
                    changed_file_count=0,
                    removed_file_count=0,
                    workers=workers,
                )
            )
        write_ms = _elapsed_ms(write_started)
        return _catalog_result(
            store,
            repo_root=repo_root,
            reused_file_count=reused_file_count,
            changed_file_count=0,
            removed_file_count=0,
            incremental=True,
            written_file_count=0,
            workers=workers,
            total_started=total_started,
            discovery_ms=discovery_ms,
            change_detection_ms=change_detection_ms,
            analysis_ms=0.0,
            write_ms=write_ms,
            planning_ms=planning_ms,
        )

    update_analyses: list[FileAnalysis] = []
    pending_analysis: list[tuple[str, Path, FileFingerprint]] = []
    reused_file_count = 0
    changed_file_count = 0

    for rel, entry in source_entries_by_rel.items():
        if rel in added_paths:
            pending_analysis.append((rel, entry.path, _fingerprint_from_entry(entry, hash_content=True)))
            changed_file_count += 1
            continue

        old_file = old_files[rel]
        fingerprint = _fingerprint_for_incremental(entry, old_file)
        should_reanalyze_dependencies = rel in dependency_reanalysis_paths
        if _fingerprint_matches(old_file, fingerprint) and not should_reanalyze_dependencies:
            reused_file_count += 1
            if int(old_file["modified_ns"]) != fingerprint.modified_ns:
                analysis = store.load_file_analysis(rel)
                if analysis is None:
                    reused_file_count -= 1
                    pending_analysis.append((rel, entry.path, fingerprint))
                    changed_file_count += 1
                    continue
                update_analyses.append(
                    replace(
                        analysis,
                        source_file=replace(
                            analysis.source_file,
                            size_bytes=fingerprint.size_bytes,
                            content_hash=fingerprint.content_hash,
                            modified_ns=fingerprint.modified_ns,
                        ),
                    )
                )
        else:
            pending_analysis.append((rel, entry.path, fingerprint))
            changed_file_count += 1
    change_detection_ms = _elapsed_ms(change_detection_started)

    analysis_ms = 0.0
    if pending_analysis:
        analysis_started = perf_counter()
        analyzed = _analyze_files(
            [(path, fingerprint) for _, path, fingerprint in pending_analysis],
            repo_root,
            all_paths,
            workers=workers,
        )
        analysis_ms = _elapsed_ms(analysis_started)
        update_analyses.extend(analyzed)

    metadata = _build_metadata(
        repo_root,
        file_count=len(source_entries),
        incremental=True,
        reused_file_count=reused_file_count,
        changed_file_count=changed_file_count,
        removed_file_count=len(removed_paths),
        workers=workers,
    )
    write_started = perf_counter()
    if not update_analyses and not removed_paths:
        store.update_meta(metadata)
    else:
        store.write_incremental_update(update_analyses, removed_paths, metadata)
    write_ms = _elapsed_ms(write_started)

    return _catalog_result(
        store,
        repo_root=repo_root,
        reused_file_count=reused_file_count,
        changed_file_count=changed_file_count,
        removed_file_count=len(removed_paths),
        incremental=True,
        written_file_count=len(update_analyses),
        workers=workers,
        total_started=total_started,
        discovery_ms=discovery_ms,
        change_detection_ms=change_detection_ms,
        analysis_ms=analysis_ms,
        write_ms=write_ms,
        planning_ms=planning_ms,
    )


def _catalog_result(
    store: CatalogStore,
    *,
    repo_root: Path,
    reused_file_count: int,
    changed_file_count: int,
    removed_file_count: int,
    incremental: bool,
    written_file_count: int,
    workers: int,
    total_started: float,
    discovery_ms: float,
    change_detection_ms: float,
    analysis_ms: float,
    write_ms: float,
    planning_ms: float = 0.0,
) -> CatalogResult:
    timings_ms = {
        "total": _elapsed_ms(total_started),
        "discovery": discovery_ms,
        "planning": planning_ms,
        "change_detection": change_detection_ms,
        "analysis": analysis_ms,
        "write": write_ms,
    }
    counts = store.catalog_counts()
    return CatalogResult(
        repo_path=str(repo_root),
        database_path=str(store.database_path),
        file_count=counts.file_count,
        symbol_count=counts.symbol_count,
        dependency_count=counts.dependency_count,
        reused_file_count=reused_file_count,
        changed_file_count=changed_file_count,
        removed_file_count=removed_file_count,
        incremental=incremental,
        written_file_count=written_file_count,
        text_line_count=counts.text_line_count,
        analysis_workers=workers,
        timings_ms=timings_ms,
    )


def _analyze_file(path: Path, repo_root: Path, all_paths: set[str], fingerprint: FileFingerprint) -> FileAnalysis:
    for analyzer in ANALYZERS:
        if analyzer.can_analyze(path):
            analysis = analyzer.analyze(path, repo_root, all_paths)
            return replace(
                analysis,
                source_file=replace(
                    analysis.source_file,
                    size_bytes=fingerprint.size_bytes,
                    content_hash=fingerprint.content_hash,
                    modified_ns=fingerprint.modified_ns,
                ),
            )
    raise ValueError(f"No analyzer registered for {path}")


def _analyze_files(
    files: list[tuple[Path, FileFingerprint]],
    repo_root: Path,
    all_paths: set[str],
    *,
    workers: int,
) -> list[FileAnalysis]:
    if not files:
        return []
    if workers <= 1 or len(files) <= 1:
        return [_analyze_file(path, repo_root, all_paths, fingerprint) for path, fingerprint in files]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(
            executor.map(
                lambda item: _analyze_file(item[0], repo_root, all_paths, item[1]),
                files,
            )
        )


def _build_metadata(
    repo_root: Path,
    analyses: list[FileAnalysis] | None = None,
    *,
    file_count: int | None = None,
    incremental: bool,
    reused_file_count: int,
    changed_file_count: int,
    removed_file_count: int,
    workers: int | None = None,
) -> dict[str, str]:
    resolved_file_count = file_count if file_count is not None else len(analyses or [])
    return _build_metadata_for_count(
        repo_root,
        file_count=resolved_file_count,
        incremental=incremental,
        reused_file_count=reused_file_count,
        changed_file_count=changed_file_count,
        removed_file_count=removed_file_count,
        workers=workers,
    )


def _dependency_reanalysis_paths(
    store: CatalogStore,
    *,
    added_paths: set[str],
    removed_paths: set[str],
    all_paths: set[str],
) -> set[str]:
    affected_paths: set[str] = set()
    if removed_paths:
        affected_paths.update(store.dependency_sources_for_targets(removed_paths))
    if added_paths:
        affected_paths.update(store.sources_with_unresolved_dependencies())
    return affected_paths & all_paths


def _build_metadata_for_count(
    repo_root: Path,
    *,
    file_count: int,
    incremental: bool,
    reused_file_count: int,
    changed_file_count: int,
    removed_file_count: int,
    workers: int | None = None,
    git_head: str | None = None,
    git_status_fingerprint: str | None = None,
) -> dict[str, str]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "analyzer_version": CATALOG_ANALYZER_VERSION,
        "repo_path": str(repo_root),
        "file_count": str(file_count),
        "git_head": git_head if git_head is not None else _git_head(repo_root),
        GIT_STATUS_FINGERPRINT_META_KEY: (
            git_status_fingerprint if git_status_fingerprint is not None else _git_status_fingerprint(repo_root)
        ),
        "scan_mode": "incremental" if incremental else "full",
        "reused_file_count": str(reused_file_count),
        "changed_file_count": str(changed_file_count),
        "removed_file_count": str(removed_file_count),
        "analysis_workers": str(_normalize_workers(workers)),
    }


def _normalize_workers(workers: int | None) -> int:
    if workers is None or workers <= 0:
        return DEFAULT_ANALYSIS_WORKERS
    return max(1, min(workers, 64))


def _effective_workers(workers: int | None, *, file_count: int) -> int:
    if workers is not None:
        return _normalize_workers(workers)
    if file_count < PARALLEL_ANALYSIS_MIN_FILES:
        return 1
    return DEFAULT_ANALYSIS_WORKERS


def _try_git_status_fast_path(
    store: CatalogStore,
    repo_root: Path,
    *,
    workers: int | None,
    skip_unchanged_meta: bool,
    total_started: float,
) -> CatalogResult | None:
    meta = store.get_meta()
    if meta.get("analyzer_version") != CATALOG_ANALYZER_VERSION:
        return None

    stored_status_fingerprint = meta.get(GIT_STATUS_FINGERPRINT_META_KEY, "")
    if not stored_status_fingerprint:
        return None

    status_started = perf_counter()
    git_head = _git_head(repo_root)
    if not git_head or meta.get("git_head") != git_head:
        return None
    current_status_fingerprint = _git_status_fingerprint(repo_root)
    status_ms = _elapsed_ms(status_started)
    if not current_status_fingerprint or current_status_fingerprint != stored_status_fingerprint:
        return None

    counts = store.catalog_counts()
    analysis_workers = _effective_workers(workers, file_count=counts.file_count)
    write_started = perf_counter()
    if not skip_unchanged_meta:
        store.update_meta(
            _build_metadata_for_count(
                repo_root,
                file_count=counts.file_count,
                incremental=True,
                reused_file_count=counts.file_count,
                changed_file_count=0,
                removed_file_count=0,
                workers=analysis_workers,
                git_head=git_head,
                git_status_fingerprint=current_status_fingerprint,
            )
        )
    write_ms = _elapsed_ms(write_started)
    result = _catalog_result(
        store,
        repo_root=repo_root,
        reused_file_count=counts.file_count,
        changed_file_count=0,
        removed_file_count=0,
        incremental=True,
        written_file_count=0,
        workers=analysis_workers,
        total_started=total_started,
        discovery_ms=0.0,
        change_detection_ms=0.0,
        analysis_ms=0.0,
        write_ms=write_ms,
        planning_ms=0.0,
    )
    result.timings_ms["git_status"] = status_ms
    result.timings_ms["fast_path"] = 1.0
    return result


def _all_file_stats_match(source_entries: list[DiscoveredSourceFile], old_files: dict[str, object]) -> bool:
    for entry in source_entries:
        old_file = old_files[entry.rel_path]
        if int(old_file["size_bytes"]) != entry.size_bytes or int(old_file["modified_ns"]) != entry.modified_ns:
            return False
    return True


def _fingerprint_for_incremental(entry: DiscoveredSourceFile, old_file: object) -> FileFingerprint:
    if int(old_file["size_bytes"]) == entry.size_bytes and int(old_file["modified_ns"]) == entry.modified_ns:
        return FileFingerprint(
            path=entry.rel_path,
            size_bytes=entry.size_bytes,
            modified_ns=entry.modified_ns,
            content_hash=str(old_file["content_hash"] or ""),
        )
    return _fingerprint_from_entry(entry, hash_content=True)


def _fingerprint_matches(old_file: object, fingerprint: FileFingerprint) -> bool:
    if (
        int(old_file["size_bytes"]) == fingerprint.size_bytes
        and int(old_file["modified_ns"]) == fingerprint.modified_ns
    ):
        return True
    old_hash = str(old_file["content_hash"] or "")
    return bool(old_hash and old_hash == fingerprint.content_hash)


def _fingerprint_from_entry(entry: DiscoveredSourceFile, *, hash_content: bool) -> FileFingerprint:
    return FileFingerprint(
        path=entry.rel_path,
        size_bytes=entry.size_bytes,
        modified_ns=entry.modified_ns,
        content_hash=_hash_file(entry.path) if hash_content else "",
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def _git_head(repo_root: Path) -> str:
    head = _git_head_from_files(repo_root)
    if head:
        return head
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _git_status_fingerprint(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude).code-intel",
                ":(exclude).code-intel/**",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    digest = hashlib.sha256()
    digest.update(result.stdout.encode())
    for gitignore_path in _ancestor_gitignore_paths(repo_root):
        digest.update(gitignore_path.as_posix().encode())
        try:
            digest.update(gitignore_path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    for rel_path in _status_source_paths(result.stdout):
        path = repo_root / rel_path
        if path.suffix not in SUPPORTED_EXTENSIONS:
            continue
        try:
            file_stat = path.stat()
        except OSError:
            digest.update(f"{rel_path}\0missing".encode())
            continue
        digest.update(f"{rel_path}\0{file_stat.st_size}\0{file_stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def _ancestor_gitignore_paths(repo_root: Path) -> list[Path]:
    git_root = _find_git_root(repo_root)
    if git_root is None:
        return [repo_root / ".gitignore"]
    try:
        rel_root = repo_root.relative_to(git_root)
    except ValueError:
        return [repo_root / ".gitignore"]
    paths = [git_root / ".gitignore"]
    for index in range(1, len(rel_root.parts) + 1):
        paths.append(git_root / Path(*rel_root.parts[:index]) / ".gitignore")
    return paths


def _git_head_from_files(repo_root: Path) -> str:
    git_dir = _find_git_dir(repo_root)
    if git_dir is None:
        return ""
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(errors="replace").strip()
    except OSError:
        return ""
    if not head.startswith("ref: "):
        return head
    ref_name = head.removeprefix("ref: ").strip()
    for ref_root in _git_ref_roots(git_dir):
        try:
            ref_value = (ref_root / ref_name).read_text(errors="replace").strip()
        except OSError:
            continue
        if ref_value:
            return ref_value
    return _packed_ref_value(git_dir, ref_name)


def _git_ref_roots(git_dir: Path) -> list[Path]:
    roots = [git_dir]
    common_dir_file = git_dir / "commondir"
    try:
        common_dir = common_dir_file.read_text(errors="replace").strip()
    except OSError:
        return roots
    resolved = (git_dir / common_dir).resolve()
    if resolved not in roots:
        roots.append(resolved)
    return roots


def _packed_ref_value(git_dir: Path, ref_name: str) -> str:
    for ref_root in _git_ref_roots(git_dir):
        packed_refs = ref_root / "packed-refs"
        try:
            lines = packed_refs.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            value, _, name = line.partition(" ")
            if name == ref_name:
                return value
    return ""


def _find_git_root(repo_root: Path) -> Path | None:
    for path in [repo_root, *repo_root.parents]:
        if (path / ".git").exists():
            return path
    return None


def _find_git_dir(repo_root: Path) -> Path | None:
    git_root = _find_git_root(repo_root)
    if git_root is None:
        return None
    dot_git = git_root / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        content = dot_git.read_text(errors="replace").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = Path(content.removeprefix("gitdir:").strip())
    if not git_dir.is_absolute():
        git_dir = dot_git.parent / git_dir
    return git_dir.resolve()


def _status_source_paths(status_output: str) -> list[str]:
    paths: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path)
    return paths
