"""Unified symbol search across code-intel and optional providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from code_intel.catalog_store import CatalogStore
from code_intel.jcodemunch_provider import search_jcodemunch_symbols

ProviderName = Literal["catalog", "jcodemunch"]


@dataclass(frozen=True, slots=True)
class SymbolSearchResult:
    """Unified symbol search response."""

    provider: str
    symbols: list[dict[str, Any]]


def search_symbols(
    repo_path: str | Path,
    store: CatalogStore,
    query: str,
    *,
    limit: int = 20,
    provider: ProviderName = "catalog",
    include_fuzzy: bool = True,
) -> SymbolSearchResult:
    """Search symbols through the selected provider."""
    if provider == "jcodemunch":
        rows = search_jcodemunch_symbols(repo_path, query, limit)
        return SymbolSearchResult(provider="jcodemunch", symbols=rows)

    if provider == "catalog":
        if not store.has_catalog():
            return SymbolSearchResult(provider="catalog", symbols=[])
        rows = [
            {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "path": row["path"],
                "line": row["line"],
                "end_line": row["end_line"],
                "signature": row["signature"],
                "summary": row["doc"],
                "provider": "catalog",
            }
            for row in store.search_symbols(query, limit, include_fuzzy=include_fuzzy)
        ]
        return SymbolSearchResult(provider="catalog", symbols=rows)

    raise ValueError(f"Unsupported provider: {provider}")
