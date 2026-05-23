"""Repository indexing orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from code_intel.analyzers import ANALYZERS, SUPPORTED_EXTENSIONS
from code_intel.discovery import discover_source_files
from code_intel.models import FileAnalysis, IndexResult
from code_intel.storage import IndexStore


def build_index(repo_path: str | Path, database_path: str | Path | None = None) -> IndexResult:
    """Build a repository index and persist it to SQLite."""
    repo_root = Path(repo_path).resolve()
    if not repo_root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {repo_root}")

    store = IndexStore.for_repo(repo_root, Path(database_path).resolve() if database_path else None)
    store.reset()

    source_files = discover_source_files(repo_root, SUPPORTED_EXTENSIONS)
    all_paths = {path.relative_to(repo_root).as_posix() for path in source_files}
    analyses = [_analyze_file(path, repo_root, all_paths) for path in source_files]

    metadata = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repo_path": str(repo_root),
        "file_count": str(len(analyses)),
    }
    store.write_index(analyses, metadata)

    return IndexResult(
        repo_path=str(repo_root),
        database_path=str(store.database_path),
        file_count=store.file_count(),
        symbol_count=store.symbol_count(),
        dependency_count=store.dependency_count(),
    )


def _analyze_file(path: Path, repo_root: Path, all_paths: set[str]) -> FileAnalysis:
    for analyzer in ANALYZERS:
        if analyzer.can_analyze(path):
            return analyzer.analyze(path, repo_root, all_paths)
    raise ValueError(f"No analyzer registered for {path}")
