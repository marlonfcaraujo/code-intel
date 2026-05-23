"""MCP server entry point for code-intel."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.change_report import compute_change_report
from code_intel.risk import top_risk_files
from code_intel.savings import (
    estimate_saved_tokens_for_paths,
    selected_paths_from_change_report,
    selected_paths_from_rows,
    selected_paths_from_test_matches,
)
from code_intel.symbol_search import ProviderName, search_symbols
from code_intel.tests_map import find_related_tests


def catalog_repo_tool(repo_path: str = ".") -> dict[str, Any]:
    """Build or refresh the code-intel catalog for a repository."""
    result = build_catalog(repo_path)
    return asdict(result)


def find_symbols_tool(
    query: str,
    repo_path: str = ".",
    limit: int = 20,
    provider: ProviderName = "auto",
) -> dict[str, Any]:
    """Search symbols in a repository through the selected provider."""
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root)
    bounded_limit = max(1, min(limit, 100))
    result = search_symbols(repo_root, store, query, limit=bounded_limit, provider=provider)
    _record_usage_event(
        store,
        tool="find_symbols",
        provider=result.provider,
        query=query,
        result_count=len(result.symbols),
        selected_paths=selected_paths_from_rows(result.symbols),
    )
    return {
        "query": query,
        "repo_path": str(repo_root),
        "provider": result.provider,
        "count": len(result.symbols),
        "symbols": [
            {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "path": row["path"],
                "line": row["line"],
                "signature": row["signature"],
                "summary": row["summary"],
            }
            for row in result.symbols
        ],
    }


def explain_file_tool(path: str, repo_path: str = ".") -> dict[str, Any]:
    """Explain the likely change impact for a cataloged file."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    report = compute_change_report(repo_root, store, path)
    _record_usage_event(
        store,
        tool="explain_file",
        provider="catalog",
        target_path=report.path,
        result_count=len(report.direct_dependents) + len(report.transitive_dependents) + len(report.related_tests),
        selected_paths=selected_paths_from_change_report(report),
    )
    return asdict(report)


def related_tests_tool(path: str, repo_path: str = ".") -> dict[str, Any]:
    """Return tests likely related to a cataloged file."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    target_path = store.resolve_file_path(path)
    if target_path is None:
        _record_usage_event(
            store,
            tool="related_tests",
            provider="catalog",
            target_path=path,
            result_count=0,
            selected_paths=set(),
        )
        return {"ok": False, "error": f"File is not cataloged: {path}", "tests": []}
    matches = find_related_tests(repo_root, store, target_path)
    _record_usage_event(
        store,
        tool="related_tests",
        provider="catalog",
        target_path=target_path,
        result_count=len(matches),
        selected_paths=selected_paths_from_test_matches(target_path, matches),
    )
    return {"ok": True, "path": target_path, "tests": [asdict(match) for match in matches]}


def risk_report_tool(repo_path: str = ".", limit: int = 20) -> dict[str, Any]:
    """Return highest-risk cataloged files."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    bounded_limit = max(1, min(limit, 100))
    rows = top_risk_files(repo_root, store, bounded_limit)
    _record_usage_event(
        store,
        tool="risk_report",
        provider="catalog",
        result_count=len(rows),
        selected_paths={row.path for row in rows},
    )
    return {"repo_path": str(repo_root), "count": len(rows), "files": [asdict(row) for row in rows]}


def catalog_health_tool(repo_path: str = ".") -> dict[str, Any]:
    """Return catalog health and counts for a repository."""
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root)
    result: dict[str, Any] = {
        "repo_path": str(repo_root),
        "catalog_path": str(store.database_path),
        "database_exists": store.database_path.exists(),
        "catalog_exists": store.has_catalog(),
    }
    if store.has_catalog():
        result.update(
            {
                "meta": store.get_meta(),
                "files": store.file_count(),
                "symbols": store.symbol_count(),
                "dependencies": store.dependency_count(),
            }
        )
    if store.database_path.exists():
        result["usage"] = store.usage_summary()
    return result


def savings_report_tool(repo_path: str = ".") -> dict[str, Any]:
    """Return aggregate usage and estimated savings for a repository."""
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root)
    return {
        "repo_path": str(repo_root),
        "catalog_path": str(store.database_path),
        **store.usage_summary(),
    }


def serve_mcp(default_repo_path: str = ".") -> None:
    """Run the code-intel MCP server over stdio."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("MCP support is not installed. Install with `code-intel[mcp]`.") from exc

    default_repo = str(Path(default_repo_path).resolve())
    server = FastMCP("code-intel")

    def repo_or_default(repo_path: str | None) -> str:
        return repo_path or default_repo

    @server.tool()
    def catalog_repo(repo_path: str | None = None) -> dict[str, Any]:
        """Build or refresh the code-intel catalog for a repository."""
        return catalog_repo_tool(repo_or_default(repo_path))

    @server.tool()
    def find_symbols(
        query: str,
        repo_path: str | None = None,
        limit: int = 20,
        provider: ProviderName = "auto",
    ) -> dict[str, Any]:
        """Search symbols in a repository through code-intel or jCodemunch."""
        return find_symbols_tool(query=query, repo_path=repo_or_default(repo_path), limit=limit, provider=provider)

    @server.tool()
    def explain_file(path: str, repo_path: str | None = None) -> dict[str, Any]:
        """Explain likely change impact before editing a file."""
        return explain_file_tool(path=path, repo_path=repo_or_default(repo_path))

    @server.tool()
    def related_tests(path: str, repo_path: str | None = None) -> dict[str, Any]:
        """Find tests likely related to a file."""
        return related_tests_tool(path=path, repo_path=repo_or_default(repo_path))

    @server.tool()
    def risk_report(repo_path: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Return highest-risk cataloged files."""
        return risk_report_tool(repo_path=repo_or_default(repo_path), limit=limit)

    @server.tool()
    def catalog_health(repo_path: str | None = None) -> dict[str, Any]:
        """Return catalog health and counts."""
        return catalog_health_tool(repo_path=repo_or_default(repo_path))

    @server.tool()
    def savings_report(repo_path: str | None = None) -> dict[str, Any]:
        """Return aggregate code-intel usage and estimated savings."""
        return savings_report_tool(repo_path=repo_or_default(repo_path))

    server.run()


def _require_catalog(repo_path: str | Path) -> CatalogStore:
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root)
    if not store.has_catalog():
        raise FileNotFoundError(f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first.")
    return store


def _record_usage_event(
    store: CatalogStore,
    *,
    tool: str,
    provider: str,
    selected_paths: set[str],
    query: str = "",
    target_path: str = "",
    result_count: int = 0,
) -> None:
    metrics = estimate_saved_tokens_for_paths(store, selected_paths, result_count)
    store.record_usage_event(
        tool=tool,
        provider=provider,
        query=query,
        target_path=target_path,
        result_count=result_count,
        **metrics,
    )
