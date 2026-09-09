"""MCP server entry point for code-intel."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from threading import Lock, get_ident
from typing import Any

from code_intel.benchmark import run_workflow_benchmark, workflow_report_to_dict, workflow_report_to_summary_dict
from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.change_report import compute_change_report
from code_intel.context_pack import (
    ContextPack,
    build_context_pack,
    build_workspace_context_pack,
    context_pack_hit_counts_by_repo,
    context_pack_selected_paths,
    context_pack_snippet_tokens_by_repo,
    context_pack_to_dict,
)
from code_intel.health import assess_catalog_health
from code_intel.lookup import lookup, lookup_selected_paths, lookup_to_dict
from code_intel.provider_config import resolve_default_symbol_provider
from code_intel.references import (
    find_references,
    reference_result_to_dict,
    reference_selected_paths,
    workspace_reference_counts_by_repo,
    workspace_reference_result_to_dict,
    workspace_reference_selected_paths,
    workspace_reference_summary_tokens_by_repo,
    workspace_reference_tokens_by_repo,
    workspace_references,
)
from code_intel.risk import top_risk_files
from code_intel.savings import (
    estimate_saved_tokens_for_context_pack,
    estimate_saved_tokens_for_paths,
    selected_paths_from_change_report,
    selected_paths_from_rows,
    selected_paths_from_test_matches,
    selected_paths_from_text_matches,
)
from code_intel.source_context import (
    get_file_content,
    get_file_outline,
    get_file_tree,
    get_repo_outline,
    get_workspace_outline,
)
from code_intel.symbol_search import ProviderName, search_symbols
from code_intel.tests_map import find_related_tests
from code_intel.workspace import (
    WorkspaceLookupResult,
    workspace_lookup,
    workspace_lookup_to_dict,
    workspace_selected_paths_by_repo,
)
from code_intel.workspace_config import (
    list_workspace_configs,
    resolve_workspace_repos,
    workspace_config_to_dict,
)
from code_intel.workspace_scan import scan_workspace, workspace_scan_to_dict

_CATALOG_STORE_CACHE_LOCK = Lock()
_CATALOG_STORE_CACHE: dict[int, dict[str, CatalogStore]] = {}


def catalog_repo_tool(
    repo_path: str = ".",
    incremental: bool = False,
    workers: int | None = None,
    skip_unchanged_meta: bool = False,
) -> dict[str, Any]:
    """Build or refresh the code-intel catalog for a repository."""
    repo_root = Path(repo_path).resolve()
    _invalidate_catalog_store(repo_root)
    result = build_catalog(
        repo_root,
        incremental=incremental,
        workers=workers,
        skip_unchanged_meta=skip_unchanged_meta,
    )
    _invalidate_catalog_store(repo_root)
    return asdict(result)


def workspace_catalog_tool(
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    incremental: bool = False,
    workers: int | None = None,
    repo_workers: int | None = None,
    skip_unchanged_meta: bool = False,
) -> dict[str, Any]:
    """Build or refresh catalogs for a multi-repository workspace."""
    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    _invalidate_catalog_stores(repo_paths)
    report = scan_workspace(
        repo_paths,
        incremental=incremental,
        workers=workers,
        repo_workers=repo_workers,
        skip_unchanged_meta=skip_unchanged_meta,
    )
    _invalidate_catalog_stores(repo_paths)
    return workspace_scan_to_dict(report)


def find_symbols_tool(
    query: str,
    repo_path: str = ".",
    limit: int = 20,
    provider: ProviderName | None = None,
) -> dict[str, Any]:
    """Search symbols in a repository through the selected provider."""
    repo_root = Path(repo_path).resolve()
    store = _cached_catalog_store(repo_root)
    provider = resolve_default_symbol_provider() if provider is None else provider
    if provider == "catalog":
        store = _require_catalog(repo_root)
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


def search_text_tool(
    query: str,
    repo_path: str = ".",
    limit: int = 20,
    context_lines: int = 1,
) -> dict[str, Any]:
    """Search indexed source text with bounded snippets."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    bounded_limit = max(1, min(limit, 100))
    bounded_context = max(0, min(context_lines, 10))
    matches = store.search_text(query, limit=bounded_limit, context_lines=bounded_context)
    _record_usage_event(
        store,
        tool="search_text",
        provider="catalog",
        query=query,
        result_count=len(matches),
        selected_paths=selected_paths_from_text_matches(matches),
    )
    return {
        "query": query,
        "repo_path": str(repo_root),
        "count": len(matches),
        "matches": [asdict(match) for match in matches],
    }


def find_references_tool(
    query: str,
    repo_path: str = ".",
    limit: int = 50,
    context_lines: int = 1,
    include_definitions: bool = True,
    ignore_case: bool = False,
    summary_only: bool = False,
) -> dict[str, Any]:
    """Find exact source references for an identifier or text fragment.

    Args:
        query: Identifier or source text fragment to find.
        repo_path: Repository root to search.
        limit: Maximum exact references to return.
        context_lines: Lines before and after each reference.
        include_definitions: Whether definition lines should be returned.
        ignore_case: Whether exact filtering should ignore case.
        summary_only: When true, return file-level summaries instead of
            per-line snippets.

    Returns:
        Serialized exact reference result with bounded snippets.
    """
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    result = find_references(
        repo_root,
        store,
        query,
        limit=limit,
        context_lines=context_lines,
        include_definitions=include_definitions,
        ignore_case=ignore_case,
    )
    _record_usage_event(
        store,
        tool="find_references",
        provider="catalog",
        query=query,
        result_count=len(result.matches),
        selected_paths=reference_selected_paths(result),
        returned_tokens=result.summary_tokens if summary_only else result.estimated_tokens,
    )
    return reference_result_to_dict(result, summary_only=summary_only)


def lookup_tool(
    query: str,
    repo_path: str = ".",
    limit: int = 20,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    context_lines: int = 0,
    source_first: bool = False,
    include_tests: bool = True,
    include_context: bool = True,
) -> dict[str, Any]:
    """Find source using an identifier or 2-4 distinctive keywords, not semantic questions.

    Non-identifier queries use field-weighted function BM25, with bounded keyword
    recovery for older catalogs. Results carry signatures and
    docstring summaries; include_context adds excerpts for the first three
    symbols, capped at 40 lines and 6000 characters each. Use context_pack for
    more body context. No-match responses include query guidance.
    """
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    result = lookup(
        repo_root,
        store,
        query,
        limit=max(1, min(limit, 100)),
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=_context_text_limit(source_first=source_first, text_limit=text_limit),
        fallback_text_limit=_context_fallback_text_limit(
            source_first=source_first,
            text_limit=text_limit,
            limit=limit,
        ),
        context_lines=context_lines,
        include_tests=_context_include_tests(source_first=source_first, include_tests=include_tests),
    )
    _record_usage_event(
        store,
        tool="lookup",
        provider="catalog",
        query=query,
        result_count=len(result.hits),
        selected_paths=lookup_selected_paths(result),
    )
    return lookup_to_dict(result, store=store if include_context else None)


def context_pack_tool(
    query: str,
    repo_path: str = ".",
    limit: int = 10,
    max_files: int = 5,
    context_lines: int = 4,
    max_lines_per_file: int = 80,
    source_first: bool = False,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    include_tests: bool = True,
    compact: bool = True,
    include_hits: bool = False,
) -> dict[str, Any]:
    """Return bounded declaration/docstring/body context for an identifier or 2-4 keywords.

    Prefer concise lexical queries; this is not semantic search. Sentence
    queries use function BM25 (or legacy keyword recovery) before no-match guidance.

    Args:
        query: Symbol, file stem, or text fragment to search.
        repo_path: Repository root to search.
        limit: Maximum ranked hits to consider.
        max_files: Maximum distinct files to include.
        context_lines: Lines before and after text hits.
        max_lines_per_file: Maximum lines returned per selected file.
        source_first: Shortcut for implementation context first. When true,
            source text search is skipped unless source/file lookup finds no
            hits, and test files are excluded.
        symbol_limit: Optional symbol hit limit.
        file_limit: Optional file path hit limit.
        text_limit: Optional source text hit limit.
        include_tests: Whether test files may be selected for snippets.
        compact: Whether to omit repeated nested repo paths and return
            repo_index references instead.
        include_hits: Whether to include ranked lookup hit rows alongside
            snippets. Defaults false to keep agent context payloads smaller.

    Returns:
        Serialized context pack with bounded source snippets.
    """
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    pack = build_context_pack(
        repo_root,
        store,
        query,
        limit=limit,
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=_context_text_limit(source_first=source_first, text_limit=text_limit),
        fallback_text_limit=_context_fallback_text_limit(
            source_first=source_first,
            text_limit=text_limit,
            limit=limit,
        ),
        include_tests=_context_include_tests(source_first=source_first, include_tests=include_tests),
    )
    _record_usage_event(
        store,
        tool="context_pack",
        provider="catalog",
        query=query,
        result_count=pack.hit_count,
        selected_paths={snippet.path for snippet in pack.snippets},
        returned_tokens=pack.estimated_tokens,
    )
    return context_pack_to_dict(pack, compact=compact, include_hits=include_hits)


def workspace_lookup_tool(
    query: str,
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    limit: int = 20,
    per_repo_limit: int | None = None,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    context_lines: int = 0,
    source_first: bool = False,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Search symbols and source text across multiple repository catalogs.

    Args:
        query: Symbol, file stem, or text fragment to search.
        repos: Explicit repository roots to search.
        workspace_name: Optional saved workspace name to include.
        workspace_dir: Optional directory for workspace JSON files.
        limit: Maximum combined hits returned.
        per_repo_limit: Maximum hits requested from each repository.
        symbol_limit: Optional symbol hit limit per repository.
        file_limit: Optional file path hit limit per repository.
        text_limit: Optional source text hit limit per repository.
        context_lines: Number of context lines for text hits.
        source_first: Shortcut for implementation results first. When true,
            source text search is skipped unless source/file lookup finds no
            hits, and test files are excluded.
        include_tests: Whether test files may be returned as lookup hits.

    Returns:
        Serialized workspace lookup result with repo-qualified hits.
    """
    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    stores_by_repo = _cached_catalog_stores(repo_paths)
    result = workspace_lookup(
        repo_paths,
        query,
        limit=max(1, min(limit, 100)),
        per_repo_limit=per_repo_limit,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=_context_text_limit(source_first=source_first, text_limit=text_limit),
        fallback_text_limit=_context_fallback_text_limit(
            source_first=source_first,
            text_limit=text_limit,
            limit=limit,
        ),
        context_lines=context_lines,
        include_tests=_context_include_tests(source_first=source_first, include_tests=include_tests),
        stores_by_repo=stores_by_repo,
    )
    _record_workspace_usage(result, query=query)
    return workspace_lookup_to_dict(result)


def workspace_references_tool(
    query: str,
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    limit: int = 50,
    per_repo_limit: int | None = None,
    context_lines: int = 1,
    include_definitions: bool = True,
    ignore_case: bool = False,
    summary_only: bool = False,
) -> dict[str, Any]:
    """Find exact source references across multiple repository catalogs.

    Args:
        query: Identifier or source text fragment to find.
        repos: Explicit repository roots to search.
        workspace_name: Optional saved workspace name to include.
        workspace_dir: Optional directory for workspace JSON files.
        limit: Maximum combined references to return.
        per_repo_limit: Maximum references requested from each repository.
        context_lines: Lines before and after each reference.
        include_definitions: Whether definition lines should be returned.
        ignore_case: Whether exact filtering should ignore case.
        summary_only: When true, return file-level summaries instead of
            per-line snippets.

    Returns:
        Serialized workspace reference result with repo-qualified snippets.
    """
    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    stores_by_repo = _cached_catalog_stores(repo_paths)
    result = workspace_references(
        repo_paths,
        query,
        limit=limit,
        per_repo_limit=per_repo_limit,
        context_lines=context_lines,
        include_definitions=include_definitions,
        ignore_case=ignore_case,
        stores_by_repo=stores_by_repo,
    )
    selected_paths_by_repo = workspace_reference_selected_paths(result)
    token_counts_by_repo = workspace_reference_tokens_by_repo(result)
    summary_token_counts_by_repo = workspace_reference_summary_tokens_by_repo(result)
    counts_by_repo = workspace_reference_counts_by_repo(result)
    for repo in result.repos:
        if not repo.searched:
            continue
        store = _cached_catalog_store(repo.repo_path)
        _record_usage_event(
            store,
            tool="workspace_references",
            provider="catalog",
            query=query,
            result_count=counts_by_repo.get(repo.repo_path, 0),
            selected_paths=selected_paths_by_repo.get(repo.repo_path, set()),
            returned_tokens=(
                summary_token_counts_by_repo.get(repo.repo_path, 0)
                if summary_only
                else token_counts_by_repo.get(repo.repo_path, 0)
            ),
        )
    return workspace_reference_result_to_dict(result, summary_only=summary_only)


def workspace_context_tool(
    query: str,
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    limit: int = 10,
    per_repo_limit: int | None = None,
    max_files: int = 5,
    context_lines: int = 4,
    max_lines_per_file: int = 80,
    source_first: bool = False,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    include_tests: bool = True,
    compact: bool = True,
    include_hits: bool = False,
) -> dict[str, Any]:
    """Return compact source context for a workspace lookup query.

    Args:
        query: Symbol, file stem, or text fragment to search.
        repos: Explicit repository roots to search.
        workspace_name: Optional saved workspace name to include.
        workspace_dir: Optional directory for workspace JSON files.
        limit: Maximum combined hits to consider.
        per_repo_limit: Maximum hits requested from each repository.
        max_files: Maximum distinct files to include.
        context_lines: Lines before and after text hits.
        max_lines_per_file: Maximum lines returned per selected file.
        source_first: Shortcut for implementation context first. When true,
            source text search is skipped unless source/file lookup finds no
            hits, and test files are excluded.
        symbol_limit: Optional symbol hit limit per repository.
        file_limit: Optional file path hit limit per repository.
        text_limit: Optional source text hit limit per repository.
        include_tests: Whether test files may be selected for snippets.
        compact: Whether to omit repeated nested repo paths and return
            repo_index references instead.
        include_hits: Whether to include ranked lookup hit rows alongside
            snippets. Defaults false to keep agent context payloads smaller.

    Returns:
        Serialized context pack with repo-qualified snippets.
    """
    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    stores_by_repo = _cached_catalog_stores(repo_paths)
    pack = _build_workspace_context_pack_for_query(
        repo_paths=repo_paths,
        stores_by_repo=stores_by_repo,
        query=query,
        limit=limit,
        per_repo_limit=per_repo_limit,
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        source_first=source_first,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=text_limit,
        include_tests=include_tests,
    )
    _record_context_pack_usage(pack, query=query, tool="workspace_context")
    return context_pack_to_dict(pack, compact=compact, include_hits=include_hits)


def workspace_context_many_tool(
    queries: list[str],
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    limit: int = 10,
    per_repo_limit: int | None = None,
    max_files: int = 5,
    context_lines: int = 4,
    max_lines_per_file: int = 80,
    source_first: bool = False,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    include_tests: bool = True,
    compact: bool = True,
    include_hits: bool = False,
    shared_snippets: bool = True,
) -> dict[str, Any]:
    """Return compact source context for several workspace lookup queries.

    Args:
        queries: Symbol, file stem, or text fragments to search.
        repos: Explicit repository roots to search.
        workspace_name: Optional saved workspace name to include.
        workspace_dir: Optional directory for workspace JSON files.
        limit: Maximum combined hits to consider per query.
        per_repo_limit: Maximum hits requested from each repository.
        max_files: Maximum distinct files to include per query.
        context_lines: Lines before and after text hits.
        max_lines_per_file: Maximum lines returned per selected file.
        source_first: Shortcut for implementation context first. When true,
            source text search is skipped unless source/file lookup finds no
            hits, and test files are excluded.
        symbol_limit: Optional symbol hit limit per repository.
        file_limit: Optional file path hit limit per repository.
        text_limit: Optional source text hit limit per repository.
        include_tests: Whether test files may be selected for snippets.
        compact: Whether to omit repeated nested repo paths and return
            repo_index references instead.
        include_hits: Whether to include ranked lookup hit rows alongside
            snippets. Defaults false to keep agent context payloads smaller.
        shared_snippets: Whether to allow snippets to move into one top-level
            table when that produces a smaller payload. Defaults true to avoid
            repeating the same source range across related queries.

    Returns:
        Serialized batch of context packs with one top-level repository map.

    Raises:
        ValueError: If no non-empty queries are supplied.
    """
    normalized_queries = [query.strip() for query in queries if query.strip()]
    if not normalized_queries:
        raise ValueError("At least one non-empty query is required.")

    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    stores_by_repo = _cached_catalog_stores(repo_paths)
    contexts: list[dict[str, Any]] = []
    repo_paths_payload: list[str] = []
    repo_labels_payload: list[str] = []
    total_hit_count = 0
    total_candidate_files = 0
    total_selected_files = 0
    total_selected_lines = 0
    total_estimated_tokens = 0
    total_estimated_saved_tokens = 0
    packs_by_query: dict[str, ContextPack] = {}
    payloads_by_query: dict[str, dict[str, Any]] = {}
    usage_packs: list[tuple[ContextPack, str]] = []
    reused_query_count = 0

    for query in normalized_queries:
        query_cache_key = _workspace_context_query_cache_key(query)
        pack = packs_by_query.get(query_cache_key)
        if pack is None:
            pack = _build_workspace_context_pack_for_query(
                repo_paths=repo_paths,
                stores_by_repo=stores_by_repo,
                query=query,
                limit=limit,
                per_repo_limit=per_repo_limit,
                max_files=max_files,
                context_lines=context_lines,
                max_lines_per_file=max_lines_per_file,
                source_first=source_first,
                symbol_limit=symbol_limit,
                file_limit=file_limit,
                text_limit=text_limit,
                include_tests=include_tests,
            )
            packs_by_query[query_cache_key] = pack
            usage_packs.append((pack, query))
        else:
            reused_query_count += 1
        cached_payload = payloads_by_query.get(query_cache_key)
        if cached_payload is None:
            cached_payload = context_pack_to_dict(pack, compact=compact, include_hits=include_hits)
            payloads_by_query[query_cache_key] = cached_payload
        payload = _clone_context_payload(cached_payload)
        payload["query"] = query
        if not repo_paths_payload:
            repo_paths_payload = list(payload["repo_paths"])
            repo_labels_payload = _repo_labels_for_batch_payload(payload, pack=pack)
        if compact:
            payload.pop("repo_paths", None)
            payload.pop("repo_labels", None)
        contexts.append(payload)
        total_hit_count += pack.hit_count
        total_candidate_files += pack.candidate_files
        total_selected_files += pack.selected_files
        total_selected_lines += pack.selected_lines
        total_estimated_tokens += pack.estimated_tokens
        total_estimated_saved_tokens += pack.estimated_saved_tokens

    _record_context_pack_usages(usage_packs, tool="workspace_context_many")

    result: dict[str, Any] = {
        "mode": "workspace-context-many",
        "query_count": len(contexts),
        "unique_query_count": len(packs_by_query),
        "reused_query_count": reused_query_count,
        "repo_paths": repo_paths_payload,
        "repo_labels": repo_labels_payload,
        "total_hit_count": total_hit_count,
        "total_candidate_files": total_candidate_files,
        "total_selected_files": total_selected_files,
        "total_selected_lines": total_selected_lines,
        "estimated_tokens": total_estimated_tokens,
        "estimated_saved_tokens": total_estimated_saved_tokens,
        "contexts": contexts,
    }
    if shared_snippets and _has_repeated_snippet_identity(contexts):
        shared_result = _batch_payload_with_shared_snippets(result)
        if _json_payload_bytes(shared_result) < _json_payload_bytes(result):
            return shared_result
    return result


def workflow_benchmark_tool(
    queries: list[str],
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    limit: int = 10,
    repeat: int = 5,
    warmup: int = 1,
    per_repo_limit: int | None = None,
    max_files: int = 5,
    context_lines: int = 4,
    max_lines_per_file: int = 80,
    source_first: bool = False,
    include_tests: bool = True,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    summary_only: bool = True,
) -> dict[str, Any]:
    """Benchmark lookup plus compact context retrieval for agent workflows.

    Args:
        queries: Symbol, file stem, or text queries to benchmark.
        repos: Explicit repository roots to search.
        workspace_name: Optional saved workspace name to include.
        workspace_dir: Optional directory for workspace JSON files.
        limit: Maximum combined hits to consider per query.
        repeat: Number of measured repetitions per query.
        warmup: Number of unmeasured warmup repetitions per query.
        per_repo_limit: Maximum hits requested from each repository.
        max_files: Maximum distinct files included in context snippets.
        context_lines: Lines before and after selected hits.
        max_lines_per_file: Maximum source lines returned per selected file.
        source_first: Shortcut for implementation context first. When true,
            source text search is skipped unless source/file lookup finds no
            hits, and test files are excluded.
        include_tests: Whether test files may be selected for snippets.
        symbol_limit: Optional symbol hit limit per repository.
        file_limit: Optional file path hit limit per repository.
        text_limit: Optional source text hit limit per repository.
        summary_only: When true, omit per-hit detail from the response.

    Returns:
        Workflow benchmark report with timings, payload bytes, selected files,
        selected source lines, and estimated avoided context.
    """
    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    report = run_workflow_benchmark(
        repo_paths,
        queries=queries,
        limit=limit,
        repeat=repeat,
        warmup=warmup,
        per_repo_limit=per_repo_limit,
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        include_tests=_context_include_tests(source_first=source_first, include_tests=include_tests),
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=_context_text_limit(source_first=source_first, text_limit=text_limit),
        fallback_text_limit=_context_fallback_text_limit(
            source_first=source_first,
            text_limit=text_limit,
            limit=limit,
        ),
    )
    return workflow_report_to_summary_dict(report) if summary_only else workflow_report_to_dict(report)


def list_workspaces_tool(workspace_dir: str | None = None) -> dict[str, Any]:
    """List named workspaces available to workspace lookup.

    Args:
        workspace_dir: Optional directory for workspace JSON files.

    Returns:
        Count and serialized workspace configurations.
    """
    configs = list_workspace_configs(config_dir=workspace_dir)
    return {"count": len(configs), "workspaces": [workspace_config_to_dict(config) for config in configs]}


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


def get_file_outline_tool(path: str, repo_path: str = ".") -> dict[str, Any]:
    """Return symbol outline for a cataloged file."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    outline = get_file_outline(repo_root, store, path)
    _record_usage_event(
        store,
        tool="get_file_outline",
        provider="catalog",
        target_path=str(outline["path"]),
        result_count=int(outline["symbol_count"]),
        selected_paths={str(outline["path"])},
    )
    return outline


def get_file_tree_tool(
    repo_path: str = ".",
    prefix: str = "",
    max_depth: int = 3,
    max_entries: int = 200,
) -> dict[str, Any]:
    """Return a compact catalog-backed file tree.

    Args:
        repo_path: Repository root to inspect.
        prefix: Optional repository-relative directory prefix.
        max_depth: Maximum path depth relative to the prefix.
        max_entries: Maximum tree entries to return.

    Returns:
        Serialized file tree with directory and file summaries.
    """
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    tree = get_file_tree(repo_root, store, prefix=prefix, max_depth=max_depth, max_entries=max_entries)
    _record_usage_event(
        store,
        tool="get_file_tree",
        provider="catalog",
        target_path=prefix,
        result_count=int(tree["returned_entries"]),
        selected_paths=_selected_paths_from_tree(tree),
        returned_tokens=_estimated_payload_tokens(tree),
    )
    return tree


def repo_outline_tool(repo_path: str = ".", max_depth: int = 2, top_files: int = 20) -> dict[str, Any]:
    """Return a compact repository outline.

    Args:
        repo_path: Repository root to inspect.
        max_depth: Directory depth to aggregate.
        top_files: Maximum symbol-heavy files to include.

    Returns:
        Repository outline with language, directory, and top-file summaries.
    """
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    outline = get_repo_outline(repo_root, store, max_depth=max_depth, top_files=top_files)
    _record_usage_event(
        store,
        tool="repo_outline",
        provider="catalog",
        result_count=len(outline["directories"]) + len(outline["top_files"]),
        selected_paths={str(row["path"]) for row in outline["top_files"]},
        returned_tokens=_estimated_payload_tokens(outline),
    )
    return outline


def workspace_outline_tool(
    repos: list[str] | None = None,
    workspace_name: str = "",
    workspace_dir: str | None = None,
    max_depth: int = 2,
    top_files: int = 10,
) -> dict[str, Any]:
    """Return a compact multi-repository outline.

    Args:
        repos: Explicit repository roots to include.
        workspace_name: Optional saved workspace name to include.
        workspace_dir: Optional directory for workspace JSON files.
        max_depth: Directory depth to aggregate per repository.
        top_files: Maximum symbol-heavy files per repository and workspace.

    Returns:
        Workspace outline with aggregate and per-repository summaries.
    """
    repo_paths = resolve_workspace_repos(repos, workspace_name=workspace_name, config_dir=workspace_dir)
    stores_by_repo = _cached_catalog_stores(repo_paths)
    outline = get_workspace_outline(repo_paths, max_depth=max_depth, top_files=top_files, stores_by_repo=stores_by_repo)
    _record_workspace_outline_usage(outline, tool="workspace_outline")
    return outline


def get_file_content_tool(
    path: str,
    repo_path: str = ".",
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Return bounded source content for a cataloged file."""
    repo_root = Path(repo_path).resolve()
    store = _require_catalog(repo_root)
    result = get_file_content(repo_root, store, path, start_line=start_line, end_line=end_line)
    _record_usage_event(
        store,
        tool="get_file_content",
        provider="catalog",
        target_path=str(result["path"]),
        result_count=1,
        selected_paths={str(result["path"])},
    )
    return result


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


def catalog_health_tool(repo_path: str = ".", summary_only: bool = False) -> dict[str, Any]:
    """Return catalog health and counts for a repository."""
    repo_root = Path(repo_path).resolve()
    store = _cached_catalog_store(repo_root)
    return assess_catalog_health(repo_root, store, summary_only=summary_only)


def savings_report_tool(repo_path: str = ".") -> dict[str, Any]:
    """Return aggregate usage and estimated savings for a repository."""
    repo_root = Path(repo_path).resolve()
    store = _cached_catalog_store(repo_root)
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
    def catalog_repo(
        repo_path: str | None = None,
        incremental: bool = False,
        workers: int | None = None,
        skip_unchanged_meta: bool = False,
    ) -> dict[str, Any]:
        """Build or refresh the code-intel catalog for a repository."""
        return catalog_repo_tool(
            repo_or_default(repo_path),
            incremental=incremental,
            workers=workers,
            skip_unchanged_meta=skip_unchanged_meta,
        )

    @server.tool()
    def workspace_catalog(
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        incremental: bool = False,
        workers: int | None = None,
        repo_workers: int | None = None,
        skip_unchanged_meta: bool = False,
    ) -> dict[str, Any]:
        """Build or refresh catalogs for a multi-repository workspace."""
        return workspace_catalog_tool(
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            incremental=incremental,
            workers=workers,
            repo_workers=repo_workers,
            skip_unchanged_meta=skip_unchanged_meta,
        )

    @server.tool()
    def find_symbols(
        query: str,
        repo_path: str | None = None,
        limit: int = 20,
        provider: ProviderName = "catalog",
    ) -> dict[str, Any]:
        """Search symbols in a repository through code-intel or an explicit compatibility provider."""
        return find_symbols_tool(query=query, repo_path=repo_or_default(repo_path), limit=limit, provider=provider)

    @server.tool()
    def search_text(
        query: str,
        repo_path: str | None = None,
        limit: int = 20,
        context_lines: int = 1,
    ) -> dict[str, Any]:
        """Search indexed source text with bounded snippets."""
        return search_text_tool(
            query=query,
            repo_path=repo_or_default(repo_path),
            limit=limit,
            context_lines=context_lines,
        )

    @server.tool()
    def find_references(
        query: str,
        repo_path: str | None = None,
        limit: int = 50,
        context_lines: int = 1,
        include_definitions: bool = True,
        ignore_case: bool = False,
        summary_only: bool = False,
    ) -> dict[str, Any]:
        """Find exact source references for an identifier or text fragment."""
        return find_references_tool(
            query=query,
            repo_path=repo_or_default(repo_path),
            limit=limit,
            context_lines=context_lines,
            include_definitions=include_definitions,
            ignore_case=ignore_case,
            summary_only=summary_only,
        )

    @server.tool()
    def lookup(
        query: str,
        repo_path: str | None = None,
        limit: int = 20,
        symbol_limit: int | None = None,
        file_limit: int | None = None,
        text_limit: int | None = None,
        context_lines: int = 0,
        source_first: bool = False,
        include_tests: bool = True,
    ) -> dict[str, Any]:
        """Search symbols and source text together."""
        return lookup_tool(
            query=query,
            repo_path=repo_or_default(repo_path),
            limit=limit,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            context_lines=context_lines,
            source_first=source_first,
            include_tests=include_tests,
        )

    @server.tool()
    def context_pack(
        query: str,
        repo_path: str | None = None,
        limit: int = 10,
        max_files: int = 5,
        context_lines: int = 4,
        max_lines_per_file: int = 80,
        source_first: bool = False,
        symbol_limit: int | None = None,
        file_limit: int | None = None,
        text_limit: int | None = None,
        include_tests: bool = True,
        include_hits: bool = False,
    ) -> dict[str, Any]:
        """Return compact source context for a lookup query."""
        return context_pack_tool(
            query=query,
            repo_path=repo_or_default(repo_path),
            limit=limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            source_first=source_first,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            include_tests=include_tests,
            include_hits=include_hits,
        )

    @server.tool()
    def workspace_lookup(
        query: str,
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        limit: int = 20,
        per_repo_limit: int | None = None,
        symbol_limit: int | None = None,
        file_limit: int | None = None,
        text_limit: int | None = None,
        context_lines: int = 0,
        source_first: bool = False,
        include_tests: bool = True,
    ) -> dict[str, Any]:
        """Search symbols and source text across multiple repository catalogs."""
        return workspace_lookup_tool(
            query=query,
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            limit=limit,
            per_repo_limit=per_repo_limit,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            context_lines=context_lines,
            source_first=source_first,
            include_tests=include_tests,
        )

    @server.tool()
    def workspace_references(
        query: str,
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        limit: int = 50,
        per_repo_limit: int | None = None,
        context_lines: int = 1,
        include_definitions: bool = True,
        ignore_case: bool = False,
        summary_only: bool = False,
    ) -> dict[str, Any]:
        """Find exact source references across multiple repository catalogs."""
        return workspace_references_tool(
            query=query,
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            limit=limit,
            per_repo_limit=per_repo_limit,
            context_lines=context_lines,
            include_definitions=include_definitions,
            ignore_case=ignore_case,
            summary_only=summary_only,
        )

    @server.tool()
    def workspace_context(
        query: str,
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        limit: int = 10,
        per_repo_limit: int | None = None,
        max_files: int = 5,
        context_lines: int = 4,
        max_lines_per_file: int = 80,
        source_first: bool = False,
        symbol_limit: int | None = None,
        file_limit: int | None = None,
        text_limit: int | None = None,
        include_tests: bool = True,
        include_hits: bool = False,
    ) -> dict[str, Any]:
        """Return compact source context for a workspace lookup query."""
        return workspace_context_tool(
            query=query,
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            limit=limit,
            per_repo_limit=per_repo_limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            source_first=source_first,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            include_tests=include_tests,
            include_hits=include_hits,
        )

    @server.tool()
    def workspace_context_many(
        queries: list[str],
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        limit: int = 10,
        per_repo_limit: int | None = None,
        max_files: int = 5,
        context_lines: int = 4,
        max_lines_per_file: int = 80,
        source_first: bool = False,
        symbol_limit: int | None = None,
        file_limit: int | None = None,
        text_limit: int | None = None,
        include_tests: bool = True,
        compact: bool = True,
        include_hits: bool = False,
        shared_snippets: bool = True,
    ) -> dict[str, Any]:
        """Return compact source context for several workspace lookup queries."""
        return workspace_context_many_tool(
            queries=queries,
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            limit=limit,
            per_repo_limit=per_repo_limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            source_first=source_first,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            include_tests=include_tests,
            compact=compact,
            include_hits=include_hits,
            shared_snippets=shared_snippets,
        )

    @server.tool()
    def workflow_benchmark(
        queries: list[str],
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        limit: int = 10,
        repeat: int = 5,
        warmup: int = 1,
        per_repo_limit: int | None = None,
        max_files: int = 5,
        context_lines: int = 4,
        max_lines_per_file: int = 80,
        source_first: bool = False,
        include_tests: bool = True,
        symbol_limit: int | None = None,
        file_limit: int | None = None,
        text_limit: int | None = None,
        summary_only: bool = True,
    ) -> dict[str, Any]:
        """Benchmark lookup plus compact context retrieval for agent workflows."""
        return workflow_benchmark_tool(
            queries=queries,
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            limit=limit,
            repeat=repeat,
            warmup=warmup,
            per_repo_limit=per_repo_limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            source_first=source_first,
            include_tests=include_tests,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            summary_only=summary_only,
        )

    @server.tool()
    def list_workspaces(workspace_dir: str | None = None) -> dict[str, Any]:
        """List named workspaces available to workspace lookup."""
        return list_workspaces_tool(workspace_dir=workspace_dir)

    @server.tool()
    def explain_file(path: str, repo_path: str | None = None) -> dict[str, Any]:
        """Explain likely change impact before editing a file."""
        return explain_file_tool(path=path, repo_path=repo_or_default(repo_path))

    @server.tool()
    def related_tests(path: str, repo_path: str | None = None) -> dict[str, Any]:
        """Find tests likely related to a file."""
        return related_tests_tool(path=path, repo_path=repo_or_default(repo_path))

    @server.tool()
    def get_file_outline(path: str, repo_path: str | None = None) -> dict[str, Any]:
        """Return symbol outline for a cataloged file."""
        return get_file_outline_tool(path=path, repo_path=repo_or_default(repo_path))

    @server.tool()
    def get_file_tree(
        repo_path: str | None = None,
        prefix: str = "",
        max_depth: int = 3,
        max_entries: int = 200,
    ) -> dict[str, Any]:
        """Return a compact catalog-backed file tree."""
        return get_file_tree_tool(
            repo_path=repo_or_default(repo_path),
            prefix=prefix,
            max_depth=max_depth,
            max_entries=max_entries,
        )

    @server.tool()
    def repo_outline(repo_path: str | None = None, max_depth: int = 2, top_files: int = 20) -> dict[str, Any]:
        """Return a compact repository outline."""
        return repo_outline_tool(repo_path=repo_or_default(repo_path), max_depth=max_depth, top_files=top_files)

    @server.tool()
    def workspace_outline(
        repos: list[str] | None = None,
        workspace_name: str = "",
        workspace_dir: str | None = None,
        max_depth: int = 2,
        top_files: int = 10,
    ) -> dict[str, Any]:
        """Return a compact multi-repository outline."""
        return workspace_outline_tool(
            repos=repos,
            workspace_name=workspace_name,
            workspace_dir=workspace_dir,
            max_depth=max_depth,
            top_files=top_files,
        )

    @server.tool()
    def get_file_content(
        path: str,
        repo_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Return bounded source content for a cataloged file."""
        return get_file_content_tool(
            path=path,
            repo_path=repo_or_default(repo_path),
            start_line=start_line,
            end_line=end_line,
        )

    @server.tool()
    def risk_report(repo_path: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Return highest-risk cataloged files."""
        return risk_report_tool(repo_path=repo_or_default(repo_path), limit=limit)

    @server.tool()
    def catalog_health(repo_path: str | None = None, summary_only: bool = False) -> dict[str, Any]:
        """Return catalog health and counts."""
        return catalog_health_tool(repo_path=repo_or_default(repo_path), summary_only=summary_only)

    @server.tool()
    def savings_report(repo_path: str | None = None) -> dict[str, Any]:
        """Return aggregate code-intel usage and estimated savings."""
        return savings_report_tool(repo_path=repo_or_default(repo_path))

    server.run()


def _require_catalog(repo_path: str | Path) -> CatalogStore:
    repo_root = Path(repo_path).resolve()
    store = _cached_catalog_store(repo_root)
    if not store.has_catalog():
        raise FileNotFoundError(f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first.")
    return store


def _cached_catalog_store(repo_path: str | Path) -> CatalogStore:
    repo_root = Path(repo_path).resolve()
    cache_key = str(repo_root)
    thread_id = get_ident()
    with _CATALOG_STORE_CACHE_LOCK:
        thread_cache = _CATALOG_STORE_CACHE.setdefault(thread_id, {})
        store = thread_cache.get(cache_key)
        if store is None:
            store = CatalogStore.for_repo(repo_root, reuse_connection=True)
            thread_cache[cache_key] = store
        return store


def _cached_catalog_stores(repo_paths: list[str | Path]) -> dict[str, CatalogStore]:
    return {str(Path(repo_path).resolve()): _cached_catalog_store(repo_path) for repo_path in repo_paths}


def _invalidate_catalog_stores(repo_paths: list[str | Path]) -> None:
    for repo_path in repo_paths:
        _invalidate_catalog_store(repo_path)


def _invalidate_catalog_store(repo_path: str | Path) -> None:
    cache_key = str(Path(repo_path).resolve())
    thread_id = get_ident()
    store: CatalogStore | None = None
    with _CATALOG_STORE_CACHE_LOCK:
        thread_cache = _CATALOG_STORE_CACHE.get(thread_id)
        if thread_cache is not None:
            store = thread_cache.pop(cache_key, None)
    if store is not None:
        store.close()


def _record_usage_event(
    store: CatalogStore,
    *,
    tool: str,
    provider: str,
    selected_paths: set[str],
    query: str = "",
    target_path: str = "",
    result_count: int = 0,
    returned_tokens: int | None = None,
) -> None:
    payload = _usage_event_payload(
        store,
        tool=tool,
        provider=provider,
        selected_paths=selected_paths,
        query=query,
        target_path=target_path,
        result_count=result_count,
        returned_tokens=returned_tokens,
    )
    if payload is None:
        return
    store.record_usage_event(**payload)


def _usage_event_payload(
    store: CatalogStore,
    *,
    tool: str,
    provider: str,
    selected_paths: set[str],
    query: str = "",
    target_path: str = "",
    result_count: int = 0,
    returned_tokens: int | None = None,
) -> dict[str, Any] | None:
    if not store.has_catalog():
        return None
    if returned_tokens is None:
        metrics = estimate_saved_tokens_for_paths(store, selected_paths, result_count)
    else:
        metrics = estimate_saved_tokens_for_context_pack(store, selected_paths, returned_tokens, result_count)
    return {
        "tool": tool,
        "provider": provider,
        "query": query,
        "target_path": target_path,
        "result_count": result_count,
        **metrics,
    }


def _build_workspace_context_pack_for_query(
    *,
    repo_paths: list[str | Path],
    stores_by_repo: dict[str, CatalogStore],
    query: str,
    limit: int,
    per_repo_limit: int | None,
    max_files: int,
    context_lines: int,
    max_lines_per_file: int,
    source_first: bool,
    symbol_limit: int | None,
    file_limit: int | None,
    text_limit: int | None,
    include_tests: bool,
) -> ContextPack:
    return build_workspace_context_pack(
        repo_paths,
        query,
        limit=limit,
        per_repo_limit=per_repo_limit,
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=_context_text_limit(source_first=source_first, text_limit=text_limit),
        fallback_text_limit=_context_fallback_text_limit(
            source_first=source_first,
            text_limit=text_limit,
            limit=limit,
        ),
        include_tests=_context_include_tests(source_first=source_first, include_tests=include_tests),
        stores_by_repo=stores_by_repo,
    )


def _repo_labels_for_batch_payload(payload: dict[str, Any], *, pack: ContextPack) -> list[str]:
    repo_labels = payload.get("repo_labels")
    if isinstance(repo_labels, list):
        return [str(label) for label in repo_labels]
    metadata = context_pack_to_dict(pack, compact=True, include_hits=False)
    labels = metadata.get("repo_labels", [])
    return [str(label) for label in labels] if isinstance(labels, list) else []


def _workspace_context_query_cache_key(query: str) -> str:
    return query.casefold()


def _clone_context_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(payload)
    snippets = cloned.get("snippets")
    if isinstance(snippets, list):
        cloned["snippets"] = [dict(snippet) if isinstance(snippet, dict) else snippet for snippet in snippets]
    hits = cloned.get("hits")
    if isinstance(hits, list):
        cloned["hits"] = [dict(hit) if isinstance(hit, dict) else hit for hit in hits]
    return cloned


def _has_repeated_snippet_identity(contexts: list[dict[str, Any]]) -> bool:
    seen: set[tuple[tuple[str, object], ...]] = set()
    for context in contexts:
        snippets = context.get("snippets", [])
        if not isinstance(snippets, list):
            continue
        for snippet in snippets:
            if not isinstance(snippet, dict):
                continue
            key = _shared_snippet_key(snippet)
            if key in seen:
                return True
            seen.add(key)
    return False


def _batch_payload_with_shared_snippets(result: dict[str, Any]) -> dict[str, Any]:
    contexts = result.get("contexts", [])
    if not isinstance(contexts, list):
        return result

    shared_snippet_rows: list[dict[str, Any]] = []
    shared_snippet_index: dict[tuple[tuple[str, object], ...], int] = {}
    shared_contexts: list[dict[str, Any]] = []
    for context in contexts:
        if not isinstance(context, dict):
            shared_contexts.append({})
            continue
        shared_context = dict(context)
        shared_context["snippet_refs"] = _move_snippets_to_shared_table(
            shared_context,
            shared_snippet_rows=shared_snippet_rows,
            shared_snippet_index=shared_snippet_index,
        )
        shared_contexts.append(shared_context)

    shared_result = {**result, "contexts": shared_contexts}
    shared_result.update(_shared_snippet_payload(shared_snippet_rows))
    return shared_result


def _move_snippets_to_shared_table(
    payload: dict[str, Any],
    *,
    shared_snippet_rows: list[dict[str, Any]],
    shared_snippet_index: dict[tuple[tuple[str, object], ...], int],
) -> list[dict[str, Any]]:
    snippets = payload.pop("snippets", [])
    if not isinstance(snippets, list):
        return []

    refs: list[dict[str, Any]] = []
    for snippet in snippets:
        if not isinstance(snippet, dict):
            continue
        key = _shared_snippet_key(snippet)
        snippet_index = shared_snippet_index.get(key)
        if snippet_index is None:
            snippet_index = len(shared_snippet_rows)
            shared_snippet_index[key] = snippet_index
            shared_snippet_rows.append({**snippet, "hit_lines": _normalized_hit_lines(snippet.get("hit_lines"))})
        else:
            shared_snippet_rows[snippet_index]["hit_lines"] = _merge_hit_lines(
                shared_snippet_rows[snippet_index].get("hit_lines"),
                snippet.get("hit_lines"),
            )

        hit_lines = _normalized_hit_lines(snippet.get("hit_lines"))
        ref: dict[str, Any] = {"snippet_index": snippet_index}
        if hit_lines:
            ref["hit_lines"] = hit_lines
        refs.append(ref)
    return refs


def _shared_snippet_key(snippet: dict[str, Any]) -> tuple[tuple[str, object], ...]:
    return tuple((key, value) for key, value in sorted(snippet.items()) if key != "hit_lines")


def _normalized_hit_lines(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    lines: set[int] = set()
    for item in value:
        try:
            lines.add(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(lines)


def _merge_hit_lines(first: object, second: object) -> list[int]:
    return sorted({*_normalized_hit_lines(first), *_normalized_hit_lines(second)})


def _shared_snippet_payload(shared_snippet_rows: list[dict[str, Any]]) -> dict[str, Any]:
    selected_files = {
        (
            row.get("repo_index", row.get("repo_path", "")),
            row.get("path", ""),
        )
        for row in shared_snippet_rows
        if row.get("path")
    }
    selected_lines = sum(
        max(0, int(row.get("end_line", 0)) - int(row.get("start_line", 0)) + 1) for row in shared_snippet_rows
    )
    return {
        "shared_snippets": True,
        "shared_snippet_count": len(shared_snippet_rows),
        "shared_selected_files": len(selected_files),
        "shared_selected_lines": selected_lines,
        "shared_estimated_tokens": _estimated_payload_tokens(shared_snippet_rows),
        "snippets": shared_snippet_rows,
    }


def _json_payload_bytes(payload: object) -> int:
    return len(json.dumps(payload, separators=(",", ":")))


def _record_workspace_usage(result: WorkspaceLookupResult, *, query: str) -> None:
    counts: dict[str, int] = {}
    for hit in result.hits:
        counts[hit.repo_path] = counts.get(hit.repo_path, 0) + 1
    for repo_path, selected_paths in workspace_selected_paths_by_repo(result).items():
        store = _cached_catalog_store(repo_path)
        _record_usage_event(
            store,
            tool="workspace_lookup",
            provider="catalog",
            query=query,
            result_count=counts.get(repo_path, 0),
            selected_paths=selected_paths,
        )


def _record_context_pack_usage(pack: ContextPack, *, query: str, tool: str) -> None:
    _record_context_pack_usages([(pack, query)], tool=tool)


def _record_context_pack_usages(usages: list[tuple[ContextPack, str]], *, tool: str) -> None:
    stores_by_repo: dict[str, CatalogStore] = {}
    events_by_repo: dict[str, list[dict[str, Any]]] = {}
    for pack, query in usages:
        _extend_context_pack_usage_events(
            events_by_repo,
            stores_by_repo=stores_by_repo,
            pack=pack,
            query=query,
            tool=tool,
        )
    for repo_path, events in events_by_repo.items():
        stores_by_repo[repo_path].record_usage_events(events)


def _extend_context_pack_usage_events(
    events_by_repo: dict[str, list[dict[str, Any]]],
    *,
    stores_by_repo: dict[str, CatalogStore],
    pack: ContextPack,
    query: str,
    tool: str,
) -> None:
    selected_paths_by_repo = context_pack_selected_paths(pack)
    token_counts_by_repo = context_pack_snippet_tokens_by_repo(pack)
    hit_counts_by_repo = context_pack_hit_counts_by_repo(pack)
    for repo_path in pack.repo_paths:
        selected_paths = selected_paths_by_repo.get(repo_path, set())
        result_count = hit_counts_by_repo.get(repo_path, 0)
        returned_tokens = token_counts_by_repo.get(repo_path, 0)
        if not selected_paths and result_count <= 0 and returned_tokens <= 0:
            continue
        store = stores_by_repo.setdefault(repo_path, _cached_catalog_store(repo_path))
        payload = _usage_event_payload(
            store,
            tool=tool,
            provider="catalog",
            query=query,
            result_count=result_count,
            selected_paths=selected_paths,
            returned_tokens=returned_tokens,
        )
        if payload is not None:
            events_by_repo.setdefault(repo_path, []).append(payload)


def _context_text_limit(*, source_first: bool, text_limit: int | None) -> int | None:
    return 0 if source_first else text_limit


def _context_fallback_text_limit(*, source_first: bool, text_limit: int | None, limit: int) -> int | None:
    if not source_first:
        return None
    if text_limit is not None:
        return text_limit
    return limit


def _context_include_tests(*, source_first: bool, include_tests: bool) -> bool:
    return False if source_first else include_tests


def _record_workspace_outline_usage(outline: dict[str, Any], *, tool: str) -> None:
    repos = outline.get("repos", [])
    if not isinstance(repos, list):
        return
    for repo in repos:
        if not isinstance(repo, dict) or not repo.get("searched"):
            continue
        store = _cached_catalog_store(str(repo["repo_path"]))
        top_files = repo.get("top_files", [])
        selected_paths = {str(row["path"]) for row in top_files if isinstance(row, dict)}
        _record_usage_event(
            store,
            tool=tool,
            provider="catalog",
            result_count=len(selected_paths),
            selected_paths=selected_paths,
            returned_tokens=_estimated_payload_tokens(repo),
        )


def _selected_paths_from_tree(tree: dict[str, Any]) -> set[str]:
    entries = tree.get("entries", [])
    if not isinstance(entries, list):
        return set()
    return {str(entry["path"]) for entry in entries if isinstance(entry, dict) and entry.get("type") == "file"}


def _estimated_payload_tokens(payload: object) -> int:
    return max(0, (len(json.dumps(payload, sort_keys=True)) + 3) // 4)
