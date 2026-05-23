"""MCP server entry point for code-intel."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.change_report import compute_change_report
from code_intel.risk import top_risk_files
from code_intel.tests_map import find_related_tests


def catalog_repo_tool(repo_path: str = ".") -> dict[str, Any]:
    """Build or refresh the code-intel catalog for a repository."""
    result = build_catalog(repo_path)
    return asdict(result)


def find_symbols_tool(query: str, repo_path: str = ".", limit: int = 20) -> dict[str, Any]:
    """Search cataloged symbols in a repository."""
    store = _require_catalog(repo_path)
    bounded_limit = max(1, min(limit, 100))
    rows = store.search_symbols(query, bounded_limit)
    return {
        "query": query,
        "repo_path": str(Path(repo_path).resolve()),
        "count": len(rows),
        "symbols": [
            {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "path": row["path"],
                "line": row["line"],
                "signature": row["signature"],
            }
            for row in rows
        ],
    }


def explain_file_tool(path: str, repo_path: str = ".") -> dict[str, Any]:
    """Explain the likely change impact for a cataloged file."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    return asdict(compute_change_report(repo_root, store, path))


def related_tests_tool(path: str, repo_path: str = ".") -> dict[str, Any]:
    """Return tests likely related to a cataloged file."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    target_path = store.resolve_file_path(path)
    if target_path is None:
        return {"ok": False, "error": f"File is not cataloged: {path}", "tests": []}
    matches = find_related_tests(repo_root, store, target_path)
    return {"ok": True, "path": target_path, "tests": [asdict(match) for match in matches]}


def risk_report_tool(repo_path: str = ".", limit: int = 20) -> dict[str, Any]:
    """Return highest-risk cataloged files."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    bounded_limit = max(1, min(limit, 100))
    rows = top_risk_files(repo_root, store, bounded_limit)
    return {"repo_path": str(repo_root), "count": len(rows), "files": [asdict(row) for row in rows]}


def catalog_health_tool(repo_path: str = ".") -> dict[str, Any]:
    """Return catalog health and counts for a repository."""
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root)
    result: dict[str, Any] = {
        "repo_path": str(repo_root),
        "catalog_path": str(store.database_path),
        "catalog_exists": store.database_path.exists(),
    }
    if store.database_path.exists():
        result.update(
            {
                "meta": store.get_meta(),
                "files": store.file_count(),
                "symbols": store.symbol_count(),
                "dependencies": store.dependency_count(),
            }
        )
    return result


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
    def find_symbols(query: str, repo_path: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Search cataloged symbols in a repository."""
        return find_symbols_tool(query=query, repo_path=repo_or_default(repo_path), limit=limit)

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

    server.run()


def _require_catalog(repo_path: str | Path) -> CatalogStore:
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root)
    if not store.database_path.exists():
        raise FileNotFoundError(f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first.")
    return store
