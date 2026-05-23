"""JavaScript and TypeScript regex analyzer."""

from __future__ import annotations

import re
from pathlib import Path

from code_intel.analyzers.base import count_lines, line_of, relative_path
from code_intel.models import Dependency, FileAnalysis, SourceFile, Symbol

_IMPORT_RE = re.compile(r"""(?:import|export)\s+(?:[^'"\n]*?\s+from\s+)?['"]([^'"]+)['"]""")
_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_IMPORT_RE = re.compile(r"""(?<!\w)import\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_SYMBOL_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE), "function", True),
    (re.compile(r"^export\s+(?:default\s+)?class\s+(\w+)", re.MULTILINE), "class", True),
    (re.compile(r"^export\s+(?:const|let|var)\s+(\w+)", re.MULTILINE), "constant", True),
    (re.compile(r"^export\s+(?:type|interface)\s+(\w+)", re.MULTILINE), "type", True),
    (re.compile(r"^(?:async\s+)?function\s+(\w+)", re.MULTILINE), "function", False),
    (re.compile(r"^class\s+(\w+)", re.MULTILINE), "class", False),
    (re.compile(r"^(?:const|let|var)\s+(\w+)\s*=", re.MULTILINE), "constant", False),
)

_EXTENSION_LANGUAGE = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
}


class JavaScriptAnalyzer:
    """Analyze JavaScript and TypeScript files with conservative regexes."""

    extensions = frozenset(_EXTENSION_LANGUAGE)

    def can_analyze(self, path: Path) -> bool:
        """Return true for JavaScript or TypeScript source files."""
        return path.suffix in self.extensions

    def analyze(self, path: Path, repo_root: Path, all_paths: set[str]) -> FileAnalysis:
        """Analyze one JavaScript or TypeScript file."""
        rel = relative_path(path, repo_root)
        source = path.read_text(errors="replace")
        source_file = SourceFile(
            path=rel,
            language=_EXTENSION_LANGUAGE.get(path.suffix, "javascript"),
            line_count=count_lines(source),
            size_bytes=path.stat().st_size,
        )
        return FileAnalysis(
            source_file=source_file,
            symbols=_extract_symbols(source, rel),
            dependencies=_extract_dependencies(source, path, repo_root, rel, all_paths),
        )


def _extract_symbols(source: str, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    seen: set[tuple[str, str]] = set()

    for pattern, kind, exported in _SYMBOL_PATTERNS:
        for match in pattern.finditer(source):
            name = match.group(1)
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            symbols.append(
                Symbol(
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    path=rel,
                    line=line_of(source, match.start()),
                    signature=match.group(0).strip(),
                    exported=exported or not name.startswith("_"),
                )
            )
    return symbols


def _extract_dependencies(
    source: str,
    file_path: Path,
    repo_root: Path,
    rel: str,
    all_paths: set[str],
) -> list[Dependency]:
    dependencies: list[Dependency] = []
    for pattern, kind in ((_IMPORT_RE, "import"), (_REQUIRE_RE, "require"), (_DYNAMIC_IMPORT_RE, "dynamic-import")):
        for match in pattern.finditer(source):
            import_name = match.group(1)
            target = _resolve_internal(import_name, file_path, repo_root, all_paths)
            dependencies.append(
                Dependency(
                    source_path=rel,
                    target_path=target or _package_name(import_name),
                    import_name=import_name,
                    kind=kind,
                    resolved=target is not None,
                )
            )
    return dependencies


def _resolve_internal(import_name: str, file_path: Path, repo_root: Path, all_paths: set[str]) -> str | None:
    if not (import_name.startswith(".") or import_name.startswith("/")):
        return None

    base = repo_root if import_name.startswith("/") else file_path.parent
    raw = (base / import_name.lstrip("/")).resolve()
    candidates = [raw]
    if raw.suffix == "":
        candidates.extend(raw.with_suffix(extension) for extension in _EXTENSION_LANGUAGE)
    candidates.extend(raw / f"index{extension}" for extension in _EXTENSION_LANGUAGE)

    for candidate in candidates:
        try:
            rel = candidate.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if rel in all_paths:
            return rel
    return None


def _package_name(import_name: str) -> str:
    if import_name.startswith("@"):
        parts = import_name.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else import_name
    return import_name.split("/")[0]
