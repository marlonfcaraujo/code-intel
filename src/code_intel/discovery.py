"""Source file discovery."""

from __future__ import annotations

import fnmatch
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

SKIP_DIR_NAMES = {
    ".code-intel",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".agents",
    ".claude",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
    ".worktree",
    ".worktrees",
}
GIT_DISCOVERY_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class DiscoveredSourceFile:
    """Source file discovered under a repository root.

    Attributes:
        path: Absolute source file path.
        rel_path: Repository-root-relative source path using POSIX separators.
        size_bytes: File size captured during discovery.
        modified_ns: File modification time captured during discovery.
    """

    path: Path
    rel_path: str
    size_bytes: int
    modified_ns: int


def discover_source_files(repo_root: Path, supported_extensions: frozenset[str]) -> list[Path]:
    """Return supported source files under ``repo_root``.

    Git repositories use ``git ls-files --cached --others --exclude-standard`` so
    `.gitignore` and global excludes are respected. Non-git directories fall
    back to a conservative recursive walk with simple `.gitignore` glob support.
    """
    return [entry.path for entry in discover_source_file_entries(repo_root, supported_extensions)]


def discover_source_file_entries(repo_root: Path, supported_extensions: frozenset[str]) -> list[DiscoveredSourceFile]:
    """Return supported source files and filesystem stats under ``repo_root``.

    Args:
        repo_root: Directory to scan. It may be a Git repository root, a
            subdirectory inside a Git repository, or a plain directory.
        supported_extensions: File suffixes that code-intel can analyze.

    Returns:
        Discovered source entries sorted by repository-relative path.
    """
    repo_root = repo_root.resolve()
    git_files = _discover_with_git(repo_root, supported_extensions)
    if git_files is not None:
        return git_files
    return _discover_with_walk(repo_root, supported_extensions)


def _discover_with_git(repo_root: Path, supported_extensions: frozenset[str]) -> list[DiscoveredSourceFile] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_DISCOVERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None

    files = []
    for rel_path in result.stdout.split("\0"):
        if not rel_path:
            continue
        entry = _source_entry_from_path(repo_root, repo_root / rel_path, supported_extensions)
        if entry is not None:
            files.append(entry)
    return sorted(files, key=lambda entry: entry.rel_path)


def _discover_with_walk(repo_root: Path, supported_extensions: frozenset[str]) -> list[DiscoveredSourceFile]:
    ignore_patterns = _load_gitignore_patterns(repo_root)
    files: list[DiscoveredSourceFile] = []
    for path in repo_root.rglob("*"):
        rel = path.relative_to(repo_root)
        if _has_skipped_part(rel):
            continue
        if path.suffix not in supported_extensions:
            continue
        if _is_ignored(rel.as_posix(), ignore_patterns):
            continue
        entry = _source_entry_from_path(repo_root, path, supported_extensions)
        if entry is not None:
            files.append(entry)
    return sorted(files, key=lambda entry: entry.rel_path)


def _load_gitignore_patterns(repo_root: Path) -> list[str]:
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns = []
    for line in gitignore.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if fnmatch.fnmatch(rel_path, normalized) or fnmatch.fnmatch(Path(rel_path).name, normalized):
            return True
        if "/" not in normalized and f"/{normalized}/" in f"/{rel_path}/":
            return True
    return False


def _has_skipped_part(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def _source_entry_from_path(
    repo_root: Path,
    path: Path,
    supported_extensions: frozenset[str],
) -> DiscoveredSourceFile | None:
    rel = path.relative_to(repo_root)
    if path.suffix not in supported_extensions or _has_skipped_part(rel):
        return None
    try:
        file_stat = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    return DiscoveredSourceFile(
        path=path,
        rel_path=rel.as_posix(),
        size_bytes=file_stat.st_size,
        modified_ns=file_stat.st_mtime_ns,
    )
