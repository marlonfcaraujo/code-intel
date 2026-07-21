"""JavaScript and TypeScript regex analyzer."""

from __future__ import annotations

import re
from pathlib import Path

from code_intel.analyzers.base import count_lines, indexed_source_lines, line_of, relative_path
from code_intel.models import Dependency, FileAnalysis, SourceFile, Symbol
from code_intel.secret_filter import redact_secret_line

_IMPORT_RE = re.compile(r"""(?:import|export)\s+(?:[^'"\n]*?\s+from\s+)?['"]([^'"]+)['"]""")
_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_IMPORT_RE = re.compile(r"""(?<!\w)import\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_IDENTIFIER_RE = r"[A-Za-z_$][\w$]*"
_ARROW_VALUE_RE = rf"(?:async\s*)?(?:\([^)]*\)|{_IDENTIFIER_RE})\s*=>"
_REACT_WRAPPER_RE = r"(?:React\.)?(?:memo|forwardRef)\s*\("

_SYMBOL_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (
        re.compile(
            rf"^export\s+default\s+{_REACT_WRAPPER_RE}\s*(?:async\s+)?function\s+({_IDENTIFIER_RE})", re.MULTILINE
        ),
        "function",
        True,
    ),
    (re.compile(rf"^export\s+default\s+(?:async\s+)?function\s+({_IDENTIFIER_RE})", re.MULTILINE), "function", True),
    (re.compile(rf"^export\s+default\s+class\s+({_IDENTIFIER_RE})", re.MULTILINE), "class", True),
    (re.compile(rf"^export\s+(?:async\s+)?function\s+({_IDENTIFIER_RE})", re.MULTILINE), "function", True),
    (re.compile(rf"^export\s+class\s+({_IDENTIFIER_RE})", re.MULTILINE), "class", True),
    (
        re.compile(rf"^export\s+(?:const|let|var)\s+({_IDENTIFIER_RE})\s*=\s*{_REACT_WRAPPER_RE}", re.MULTILINE),
        "function",
        True,
    ),
    (
        re.compile(rf"^export\s+(?:const|let|var)\s+({_IDENTIFIER_RE})\s*=\s*{_ARROW_VALUE_RE}", re.MULTILINE),
        "function",
        True,
    ),
    (re.compile(rf"^export\s+(?:const|let|var)\s+({_IDENTIFIER_RE})", re.MULTILINE), "constant", True),
    (re.compile(rf"^export\s+(?:type|interface)\s+({_IDENTIFIER_RE})", re.MULTILINE), "type", True),
    (re.compile(rf"^(?:async\s+)?function\s+({_IDENTIFIER_RE})", re.MULTILINE), "function", False),
    (re.compile(rf"^class\s+({_IDENTIFIER_RE})", re.MULTILINE), "class", False),
    (
        re.compile(rf"^(?:const|let|var)\s+({_IDENTIFIER_RE})\s*=\s*{_REACT_WRAPPER_RE}", re.MULTILINE),
        "function",
        False,
    ),
    (re.compile(rf"^(?:const|let|var)\s+({_IDENTIFIER_RE})\s*=\s*{_ARROW_VALUE_RE}", re.MULTILINE), "function", False),
    (re.compile(rf"^(?:const|let|var)\s+({_IDENTIFIER_RE})\s*=", re.MULTILINE), "constant", False),
)

_EXTENSION_LANGUAGE = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
}
_RESOLVABLE_SOURCE_EXTENSIONS = (*_EXTENSION_LANGUAGE, ".css")
_STATIC_ASSET_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".eot",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".json",
        ".mp4",
        ".png",
        ".svg",
        ".ttf",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
    }
)


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
            text_lines=indexed_source_lines(rel, source),
        )


def _extract_symbols(source: str, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    seen: set[str] = set()

    for pattern, kind, exported in _SYMBOL_PATTERNS:
        for match in pattern.finditer(source):
            name = match.group(1)
            if name in seen:
                continue
            seen.add(name)
            symbols.append(
                Symbol(
                    name=name,
                    qualified_name=name,
                    kind=_classify_symbol_kind(kind, name),
                    path=rel,
                    line=line_of(source, match.start()),
                    signature=redact_secret_line(match.group(0).strip()),
                    exported=exported or not name.startswith("_"),
                )
            )
    return symbols


def _classify_symbol_kind(base_kind: str, name: str) -> str:
    if base_kind in {"class", "function"}:
        if _is_hook_name(name):
            return "hook"
        if _is_component_name(name):
            return "component"
    return base_kind


def _is_hook_name(name: str) -> bool:
    return bool(re.fullmatch(r"use[A-Z]\w*", name))


def _is_component_name(name: str) -> bool:
    return bool(name and name[0].isupper() and any(character.islower() for character in name))


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
            asset = None if target else _resolve_asset(import_name, file_path, repo_root)
            category = _dependency_category(import_name, target, asset)
            dependencies.append(
                Dependency(
                    source_path=rel,
                    target_path=target or asset or _target_name_for_unresolved(import_name),
                    import_name=import_name,
                    kind=kind,
                    resolved=target is not None,
                    category=category,
                )
            )
    return dependencies


def _resolve_internal(import_name: str, file_path: Path, repo_root: Path, all_paths: set[str]) -> str | None:
    import_path = _strip_import_query(import_name)
    if not (import_path.startswith(".") or import_path.startswith("/")):
        return None

    base = repo_root if import_path.startswith("/") else file_path.parent
    raw = (base / import_path.lstrip("/")).resolve()
    candidates = [raw]
    if raw.suffix == "":
        candidates.extend(raw.with_suffix(extension) for extension in _RESOLVABLE_SOURCE_EXTENSIONS)
    candidates.extend(raw / f"index{extension}" for extension in _RESOLVABLE_SOURCE_EXTENSIONS)

    for candidate in candidates:
        try:
            rel = candidate.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if rel in all_paths:
            return rel
    return None


def _resolve_asset(import_name: str, file_path: Path, repo_root: Path) -> str | None:
    import_path = _strip_import_query(import_name)
    if not (import_path.startswith(".") or import_path.startswith("/")):
        return None

    base = repo_root if import_path.startswith("/") else file_path.parent
    raw = (base / import_path.lstrip("/")).resolve()
    if raw.suffix.lower() not in _STATIC_ASSET_EXTENSIONS or not raw.is_file():
        return None
    try:
        return raw.relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _dependency_category(import_name: str, target: str | None, asset: str | None) -> str:
    if target is not None:
        return "code"
    if asset is not None:
        return "asset"
    if import_name.startswith(".") or import_name.startswith("/"):
        return "unresolved"
    return "external"


def _target_name_for_unresolved(import_name: str) -> str:
    if import_name.startswith(".") or import_name.startswith("/"):
        return _strip_import_query(import_name)
    return _package_name(import_name)


def _strip_import_query(import_name: str) -> str:
    return import_name.split("?", 1)[0].split("#", 1)[0]


def _package_name(import_name: str) -> str:
    if import_name.startswith("@"):
        parts = import_name.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else import_name
    return import_name.split("/")[0]
