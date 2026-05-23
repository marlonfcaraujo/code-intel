"""Python AST analyzer."""

from __future__ import annotations

import ast
from pathlib import Path

from code_intel.analyzers.base import count_lines, relative_path
from code_intel.models import Dependency, FileAnalysis, SourceFile, Symbol


class PythonAnalyzer:
    """Analyze Python files using the standard library AST."""

    extensions = frozenset({".py"})

    def can_analyze(self, path: Path) -> bool:
        """Return true for Python source files."""
        return path.suffix == ".py"

    def analyze(self, path: Path, repo_root: Path, all_paths: set[str]) -> FileAnalysis:
        """Analyze one Python file."""
        rel = relative_path(path, repo_root)
        source = path.read_text(errors="replace")
        source_file = SourceFile(
            path=rel,
            language="python",
            line_count=count_lines(source),
            size_bytes=path.stat().st_size,
        )

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return FileAnalysis(source_file=source_file)

        symbols = _extract_symbols(tree, source, rel)
        dependencies = _extract_dependencies(tree, path, repo_root, rel, all_paths)
        return FileAnalysis(source_file=source_file, symbols=symbols, dependencies=dependencies)


def _extract_symbols(tree: ast.Module, source: str, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    lines = source.splitlines()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_symbol_from_node(node, rel, "function", node.name, lines))
        elif isinstance(node, ast.ClassDef):
            symbols.append(_symbol_from_node(node, rel, "class", node.name, lines))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        _symbol_from_node(
                            child,
                            rel,
                            "method",
                            child.name,
                            lines,
                            qualified_name=f"{node.name}.{child.name}",
                        )
                    )

    return symbols


def _symbol_from_node(
    node: ast.AST,
    rel: str,
    kind: str,
    name: str,
    lines: list[str],
    *,
    qualified_name: str | None = None,
) -> Symbol:
    line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", None)
    signature = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    doc = ast.get_docstring(node) or ""
    return Symbol(
        name=name,
        qualified_name=qualified_name or name,
        kind=kind,
        path=rel,
        line=line,
        end_line=end_line,
        signature=signature,
        doc=doc.splitlines()[0] if doc else "",
        exported=not name.startswith("_"),
    )


def _extract_dependencies(
    tree: ast.Module,
    file_path: Path,
    repo_root: Path,
    rel: str,
    all_paths: set[str],
) -> list[Dependency]:
    dependencies: list[Dependency] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dependencies.append(_dependency_for_import(rel, alias.name, file_path, repo_root, all_paths))
        elif isinstance(node, ast.ImportFrom):
            module_name = "." * node.level + (node.module or "")
            if node.module:
                dependencies.append(_dependency_for_import(rel, module_name, file_path, repo_root, all_paths))
            else:
                for alias in node.names:
                    dependencies.append(
                        _dependency_for_import(rel, "." * node.level + alias.name, file_path, repo_root, all_paths)
                    )
    return dependencies


def _dependency_for_import(
    source_path: str,
    import_name: str,
    file_path: Path,
    repo_root: Path,
    all_paths: set[str],
) -> Dependency:
    target = _resolve_internal(import_name, file_path, repo_root, all_paths)
    return Dependency(
        source_path=source_path,
        target_path=target or import_name.split(".")[0].lstrip("."),
        import_name=import_name,
        kind="import",
        resolved=target is not None,
    )


def _resolve_internal(import_name: str, file_path: Path, repo_root: Path, all_paths: set[str]) -> str | None:
    if import_name.startswith("."):
        level = len(import_name) - len(import_name.lstrip("."))
        module_part = import_name[level:]
        base = file_path.parent
        for _ in range(max(level - 1, 0)):
            base = base.parent
        candidates = _relative_module_candidates(base, module_part, repo_root)
    else:
        candidates = _absolute_module_candidates(import_name)
        package_base = file_path.parent
        try:
            package_rel = package_base.relative_to(repo_root).as_posix()
        except ValueError:
            package_rel = ""
        if package_rel:
            candidates.extend(f"{package_rel}/{candidate}" for candidate in list(candidates))

    for candidate in candidates:
        if candidate in all_paths:
            return candidate
    return None


def _relative_module_candidates(base: Path, module_part: str, repo_root: Path) -> list[str]:
    target = base.joinpath(*module_part.split(".")) if module_part else base
    try:
        rel = target.relative_to(repo_root).as_posix()
    except ValueError:
        return []
    if not rel or rel == ".":
        return ["__init__.py"]
    return [f"{rel}.py", f"{rel}/__init__.py"]


def _absolute_module_candidates(import_name: str) -> list[str]:
    rel = "/".join(part for part in import_name.split(".") if part)
    return [f"{rel}.py", f"{rel}/__init__.py", f"src/{rel}.py", f"src/{rel}/__init__.py"]
