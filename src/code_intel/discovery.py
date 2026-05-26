"""Source file discovery."""

from __future__ import annotations

import fnmatch
import subprocess
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


def discover_source_files(repo_root: Path, supported_extensions: frozenset[str]) -> list[Path]:
    """Return supported source files under ``repo_root``.

    Git repositories use ``git ls-files --cached --others --exclude-standard`` so
    `.gitignore` and global excludes are respected. Non-git directories fall
    back to a conservative recursive walk with simple `.gitignore` glob support.
    """
    repo_root = repo_root.resolve()
    git_files = _discover_with_git(repo_root, supported_extensions)
    if git_files is not None:
        return git_files
    return _discover_with_walk(repo_root, supported_extensions)


def _discover_with_git(repo_root: Path, supported_extensions: frozenset[str]) -> list[Path] | None:
    if not (repo_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    files = []
    for line in result.stdout.splitlines():
        path = repo_root / line
        if (
            path.is_file()
            and path.suffix in supported_extensions
            and not _has_skipped_part(path.relative_to(repo_root))
        ):
            files.append(path)
    return sorted(files)


def _discover_with_walk(repo_root: Path, supported_extensions: frozenset[str]) -> list[Path]:
    ignore_patterns = _load_gitignore_patterns(repo_root)
    files: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if _has_skipped_part(rel):
            continue
        if path.suffix not in supported_extensions:
            continue
        if _is_ignored(rel.as_posix(), ignore_patterns):
            continue
        files.append(path)
    return sorted(files)


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
