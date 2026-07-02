"""Heuristics for mapping source files to related tests."""

from __future__ import annotations

from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.models import TestMatch

TEST_PATH_PARTS = {"test", "tests", "__tests__"}
TEST_SUFFIXES = (".test.js", ".test.jsx", ".test.ts", ".test.tsx", ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx")
GENERIC_SOURCE_STEMS = {"__init__", "client", "config", "constants", "endpoints", "errors", "models", "types", "utils"}
PATH_CONTEXT_STOPWORDS = {
    "__tests__",
    "src",
    "test",
    "tests",
    "unit",
}


def find_related_tests(repo_root: Path, store: CatalogStore, target_path: str) -> list[TestMatch]:
    """Find tests likely related to ``target_path``."""
    matches: dict[str, set[str]] = {}
    target_stem = Path(target_path).stem

    for file_row in store.list_files():
        path = str(file_row["path"])
        if not _is_test_path(path):
            continue

        if _stem_matches(path, target_stem) and _stem_match_is_relevant(target_path, path, target_stem):
            matches.setdefault(path, set()).add("name matches source file")

        dependencies = store.dependencies_for_file(path)
        if any(str(dep["target_path"]) == target_path for dep in dependencies):
            matches.setdefault(path, set()).add("imports source file")

        if _mentions_symbol(repo_root, store, target_path, path):
            matches.setdefault(path, set()).add("mentions source symbol")

    return [
        TestMatch(path=path, reason=", ".join(sorted(reasons)))
        for path, reasons in sorted(matches.items(), key=lambda item: item[0])
    ]


def _is_test_path(path: str) -> bool:
    parts = set(Path(path).parts)
    name = Path(path).name
    return bool(parts & TEST_PATH_PARTS) or name.startswith("test_") or name.endswith(TEST_SUFFIXES)


def _stem_matches(path: str, target_stem: str) -> bool:
    test_name = Path(path).name
    normalized = test_name.removeprefix("test_")
    for suffix in TEST_SUFFIXES:
        normalized = normalized.removesuffix(suffix)
    normalized = normalized.rsplit(".", 1)[0]
    return target_stem in {normalized, normalized.removeprefix("test_")}


def _stem_match_is_relevant(target_path: str, test_path: str, target_stem: str) -> bool:
    if target_stem not in GENERIC_SOURCE_STEMS:
        return True
    return bool(_path_context_tokens(target_path) & _path_context_tokens(test_path))


def _path_context_tokens(path: str) -> set[str]:
    tokens = set(Path(path).with_suffix("").parts)
    cleaned = {
        token.removeprefix("test_")
        for token in tokens
        if token not in PATH_CONTEXT_STOPWORDS and token not in GENERIC_SOURCE_STEMS
    }
    return {token for token in cleaned if token}


def _mentions_symbol(repo_root: Path, store: CatalogStore, target_path: str, test_path: str) -> bool:
    full_path = repo_root / test_path
    try:
        source = full_path.read_text(errors="replace")
    except OSError:
        return False
    symbols = [str(row["name"]) for row in store.symbols_for_file(target_path)]
    public_symbols = [symbol for symbol in symbols if symbol and not symbol.startswith("_")]
    return any(symbol in source for symbol in public_symbols[:50])
