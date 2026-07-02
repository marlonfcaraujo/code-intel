"""Base analyzer protocols and helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from code_intel.models import FileAnalysis, TextLine
from code_intel.secret_filter import redact_secret_line


class Analyzer(Protocol):
    """Protocol implemented by language analyzers."""

    extensions: frozenset[str]

    def can_analyze(self, path: Path) -> bool:
        """Return true when the analyzer supports ``path``."""
        ...

    def analyze(self, path: Path, repo_root: Path, all_paths: set[str]) -> FileAnalysis:
        """Analyze one file."""
        ...


def relative_path(path: Path, repo_root: Path) -> str:
    """Return a repository-relative POSIX path."""
    return path.relative_to(repo_root).as_posix()


def count_lines(source: str) -> int:
    """Return a stable line count for source text."""
    if not source:
        return 0
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def line_of(source: str, offset: int) -> int:
    """Return a one-based line number for ``offset`` in ``source``."""
    return source[:offset].count("\n") + 1


def indexed_source_lines(path: str, source: str) -> list[TextLine]:
    """Return non-empty source lines for text indexing.

    Args:
        path: Cataloged source path.
        source: Source text read by the analyzer.

    Returns:
        Non-empty source lines with one-based line numbers.
    """
    return [
        TextLine(path=path, line=line_number, content=redact_secret_line(line))
        for line_number, line in enumerate(source.splitlines(), start=1)
        if line.strip()
    ]
