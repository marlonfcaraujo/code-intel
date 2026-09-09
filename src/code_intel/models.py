"""Typed data models used by code-intel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Language = Literal["python", "javascript", "typescript", "tsx", "jsx", "css"]
SymbolKind = Literal["class", "function", "method", "constant", "type", "component", "hook", "selector", "keyframes"]
DependencyKind = Literal["import", "require", "dynamic-import"]
DependencyCategory = Literal["code", "stdlib", "external", "asset", "unresolved"]


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A source file included in the repository catalog."""

    path: str
    language: str
    line_count: int
    size_bytes: int
    content_hash: str = ""
    modified_ns: int = 0


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
    full_doc: str = ""


@dataclass(frozen=True, slots=True)
class Dependency:
    """A dependency edge from one file to another file or external module."""

    source_path: str
    target_path: str
    import_name: str
    kind: str
    resolved: bool
    category: str = "code"


@dataclass(frozen=True, slots=True)
class TextLine:
    """One non-empty source line used for text indexing."""

    path: str
    line: int
    content: str


@dataclass(frozen=True, slots=True)
class FileAnalysis:
    """Analysis result for one source file."""

    source_file: SourceFile
    symbols: list[Symbol] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    text_lines: list[TextLine] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TextMatch:
    """A source text match from the catalog text index."""

    path: str
    line: int
    language: str
    content: str
    snippet: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class CatalogResult:
    """Summary of a completed catalog run."""

    repo_path: str
    database_path: str
    file_count: int
    symbol_count: int
    dependency_count: int
    reused_file_count: int = 0
    changed_file_count: int = 0
    removed_file_count: int = 0
    incremental: bool = False
    written_file_count: int = 0
    text_line_count: int = 0
    analysis_workers: int = 0
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TestMatch:
    """A test file likely related to a source file."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ChangeReport:
    """Change report for a source file."""

    path: str
    direct_dependents: list[str]
    transitive_dependents: list[str]
    related_tests: list[TestMatch]
    blast_score: float
    risk: str
