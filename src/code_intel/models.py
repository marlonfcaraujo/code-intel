"""Typed data models used by code-intel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Language = Literal["python", "javascript", "typescript", "tsx", "jsx"]
SymbolKind = Literal["class", "function", "method", "constant", "type"]
DependencyKind = Literal["import", "require", "dynamic-import"]


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A source file included in the repository index."""

    path: str
    language: str
    line_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Symbol:
    """A symbol discovered in a source file."""

    name: str
    qualified_name: str
    kind: str
    path: str
    line: int
    end_line: int | None = None
    signature: str = ""
    doc: str = ""
    exported: bool = True


@dataclass(frozen=True, slots=True)
class Dependency:
    """A dependency edge from one file to another file or external module."""

    source_path: str
    target_path: str
    import_name: str
    kind: str
    resolved: bool


@dataclass(frozen=True, slots=True)
class FileAnalysis:
    """Analysis result for one source file."""

    source_file: SourceFile
    symbols: list[Symbol] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Summary of a completed indexing run."""

    repo_path: str
    database_path: str
    file_count: int
    symbol_count: int
    dependency_count: int


@dataclass(frozen=True, slots=True)
class TestMatch:
    """A test file likely related to a source file."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImpactReport:
    """Impact report for a source file."""

    path: str
    direct_dependents: list[str]
    transitive_dependents: list[str]
    related_tests: list[TestMatch]
    blast_score: float
    risk: str
