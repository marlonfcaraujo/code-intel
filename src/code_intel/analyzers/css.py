"""CSS analyzer for UI source catalogs."""

from __future__ import annotations

import re
from pathlib import Path

from code_intel.analyzers.base import count_lines, indexed_source_lines, line_of, relative_path
from code_intel.models import FileAnalysis, SourceFile, Symbol

_KEYFRAMES_RE = re.compile(r"@keyframes\s+([A-Za-z_][\w-]*)")
_CLASS_SELECTOR_RE = re.compile(r"(?<![\w-])\.([A-Za-z_][\w-]*)")


class CssAnalyzer:
    """Analyze CSS files with lightweight selector extraction."""

    extensions = frozenset({".css"})

    def can_analyze(self, path: Path) -> bool:
        """Return true for CSS source files."""
        return path.suffix == ".css"

    def analyze(self, path: Path, repo_root: Path, all_paths: set[str]) -> FileAnalysis:
        """Analyze one CSS file.

        Args:
            path: CSS file to inspect.
            repo_root: Repository root used for relative paths.
            all_paths: All cataloged source paths. CSS does not currently use
                this value but keeps the analyzer protocol consistent.

        Returns:
            File analysis with class selectors and keyframes as searchable
            symbols.
        """
        _ = all_paths
        rel = relative_path(path, repo_root)
        source = path.read_text(errors="replace")
        return FileAnalysis(
            source_file=SourceFile(
                path=rel,
                language="css",
                line_count=count_lines(source),
                size_bytes=path.stat().st_size,
            ),
            symbols=_extract_symbols(source, rel),
            text_lines=indexed_source_lines(rel, source),
        )


def _extract_symbols(source: str, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    seen: set[tuple[str, str]] = set()
    for pattern, kind, prefix in ((_KEYFRAMES_RE, "keyframes", "@keyframes "), (_CLASS_SELECTOR_RE, "selector", ".")):
        for match in pattern.finditer(source):
            name = match.group(1)
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            symbols.append(
                Symbol(
                    name=f"{prefix}{name}",
                    qualified_name=f"{prefix}{name}",
                    kind=kind,
                    path=rel,
                    line=line_of(source, match.start()),
                    signature=match.group(0).strip(),
                    exported=True,
                )
            )
    return sorted(symbols, key=lambda symbol: (symbol.line, symbol.name))
