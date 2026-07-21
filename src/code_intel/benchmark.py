"""Real-world benchmark helpers for code-intel providers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Literal

from code_intel.catalog_store import CatalogStore
from code_intel.provider_config import DEFAULT_BENCHMARK_PROVIDERS, ProviderName
from code_intel.context_pack import (
    ContextPack,
    build_context_pack,
    build_workspace_context_pack,
    context_pack_to_dict,
)
from code_intel.jcodemunch_provider import get_jcodemunch_catalog_stats
from code_intel.lookup import lookup, lookup_selected_paths, lookup_to_dict
from code_intel.savings import (
    estimate_saved_tokens_for_paths,
    selected_paths_from_rows,
    selected_paths_from_text_matches,
)
from code_intel.symbol_search import search_symbols
from code_intel.workspace import workspace_lookup, workspace_lookup_to_dict, workspace_selected_paths_by_repo

DEFAULT_BENCHMARK_LIMIT = 20
DEFAULT_BENCHMARK_REPEAT = 5
DEFAULT_BENCHMARK_WARMUP = 1
JCODEMUNCH_HEALTH_KEYS = (
    "database_path",
    "available",
    "files",
    "symbols",
    "repo",
    "source_root",
    "git_head",
    "indexed_at",
    "languages",
)
JCODEMUNCH_META_SUMMARY_KEYS = (
    "display_name",
    "index_version",
    "owner",
    "package_names",
    "source_roots",
)
BenchmarkMode = Literal["symbol", "text", "lookup"]


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Health summary for one benchmark provider."""

    provider: str
    available: bool
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class QueryRun:
    """Measured result for one provider/query pair."""

    provider: str
    mode: str
    query: str
    repeat: int
    result_count: int
    avg_ms: float
    median_ms: float
    min_ms: float
    max_ms: float
    selected_paths: list[str]
    top_results: list[dict[str, object]]
    estimated_saved_tokens: int
    candidate_files: int
    returned_files: int
    source_path_count: int
    test_path_count: int
    first_result_path: str
    first_result_kind: str
    first_result_is_test: bool


@dataclass(frozen=True, slots=True)
class QueryComparison:
    """Provider comparison for one benchmark query."""

    query: str
    runs: list[QueryRun]
    overlap: dict[str, object]


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Complete benchmark report for a repository."""

    repo_path: str
    mode: str
    limit: int
    repeat: int
    warmup: int
    providers: list[str]
    health: list[ProviderHealth]
    queries: list[QueryComparison]


@dataclass(frozen=True, slots=True)
class WorkspaceBenchmarkReport:
    """Benchmark report for lookup across several repository catalogs."""

    repo_paths: list[str]
    mode: str
    limit: int
    repeat: int
    warmup: int
    health: list[ProviderHealth]
    queries: list[QueryComparison]


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Measured context-gathering workflow for one query.

    Attributes:
        query: User query used for lookup and context selection.
        repeat: Number of measured iterations.
        avg_ms: Average elapsed time in milliseconds.
        median_ms: Median elapsed time in milliseconds.
        min_ms: Fastest measured elapsed time in milliseconds.
        max_ms: Slowest measured elapsed time in milliseconds.
        hit_count: Ranked lookup hit count considered by the context pack.
        selected_files: Number of distinct files represented by snippets.
        selected_lines: Number of source lines returned across snippets.
        estimated_tokens: Directional token estimate for returned snippets.
        estimated_saved_tokens: Directional token estimate avoided versus
            broad repository reads.
        candidate_files: Number of cataloged files searched.
        payload_bytes: UTF-8 byte count of the full context-pack JSON payload.
        selected_paths: Source paths represented by returned snippets.
        top_hits: Trimmed top ranked hits from the lookup phase.
    """

    query: str
    repeat: int
    avg_ms: float
    median_ms: float
    min_ms: float
    max_ms: float
    hit_count: int
    selected_files: int
    selected_lines: int
    estimated_tokens: int
    estimated_saved_tokens: int
    candidate_files: int
    payload_bytes: int
    selected_paths: list[str]
    top_hits: list[dict[str, object]]
    source_path_count: int
    test_path_count: int
    first_result_path: str
    first_result_kind: str
    first_result_is_test: bool


@dataclass(frozen=True, slots=True)
class WorkflowBenchmarkReport:
    """Benchmark report for the lookup-to-context agent workflow.

    Attributes:
        repo_paths: Repository roots searched.
        mode: Workflow mode measured by the report.
        limit: Maximum ranked hits considered per query.
        repeat: Number of measured iterations per query.
        warmup: Number of unmeasured warmup iterations per query.
        per_repo_limit: Maximum hits requested per repository, when supplied.
        max_files: Maximum snippet files selected per query.
        context_lines: Lines before and after selected hits.
        max_lines_per_file: Maximum snippet lines returned per file.
        include_tests: Whether test files may be selected for snippets.
        health: Per-catalog health summary.
        queries: Measured workflow runs.
    """

    repo_paths: list[str]
    mode: str
    limit: int
    repeat: int
    warmup: int
    per_repo_limit: int | None
    max_files: int
    context_lines: int
    max_lines_per_file: int
    include_tests: bool
    health: list[ProviderHealth]
    queries: list[WorkflowRun]


def run_benchmark(
    repo_path: str | Path,
    *,
    queries: list[str] | None = None,
    providers: tuple[ProviderName, ...] = DEFAULT_BENCHMARK_PROVIDERS,
    mode: BenchmarkMode = "symbol",
    limit: int = DEFAULT_BENCHMARK_LIMIT,
    repeat: int = DEFAULT_BENCHMARK_REPEAT,
    warmup: int = DEFAULT_BENCHMARK_WARMUP,
) -> BenchmarkReport:
    """Run repeated real search queries against configured providers.

    Args:
        repo_path: Repository path whose providers should be benchmarked.
        queries: Search queries to run. When omitted, representative symbol
            names are sampled from the code-intel catalog.
        providers: Provider names to compare. Non-symbol modes benchmark the
            built-in catalog workflow because jCodemunch only exposes comparable
            symbol search in this tool.
        mode: Benchmark mode: ``symbol``, ``text``, or ``lookup``.
        limit: Maximum results requested from each provider.
        repeat: Number of measured iterations per query/provider.
        warmup: Number of unmeasured warmup iterations per query/provider.

    Returns:
        Benchmark report with provider health, timing, overlap, and estimated
        context savings.

    Raises:
        FileNotFoundError: If no code-intel catalog exists for the repository.
        ValueError: If no queries are supplied or discoverable.
    """
    repo_root = Path(repo_path).resolve()
    store = CatalogStore.for_repo(repo_root, reuse_connection=True)
    if not store.has_catalog():
        raise FileNotFoundError(f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first.")

    bounded_limit = max(1, min(limit, 100))
    bounded_repeat = max(1, repeat)
    bounded_warmup = max(0, warmup)
    benchmark_mode = _normalize_mode(mode)
    benchmark_providers = _providers_for_mode(benchmark_mode, providers)
    benchmark_queries = _normalize_queries(queries) or _sample_catalog_queries(store, bounded_limit)
    if not benchmark_queries:
        raise ValueError("No benchmark queries were provided or discoverable from the catalog.")

    health = [_provider_health(provider, repo_root, store) for provider in benchmark_providers]
    comparisons = [
        _benchmark_query(
            repo_root,
            store,
            query,
            providers=benchmark_providers,
            mode=benchmark_mode,
            limit=bounded_limit,
            repeat=bounded_repeat,
            warmup=bounded_warmup,
        )
        for query in benchmark_queries
    ]
    return BenchmarkReport(
        repo_path=str(repo_root),
        mode=benchmark_mode,
        limit=bounded_limit,
        repeat=bounded_repeat,
        warmup=bounded_warmup,
        providers=list(benchmark_providers),
        health=health,
        queries=comparisons,
    )


def run_workspace_benchmark(
    repo_paths: list[str | Path],
    *,
    queries: list[str],
    limit: int = DEFAULT_BENCHMARK_LIMIT,
    repeat: int = DEFAULT_BENCHMARK_REPEAT,
    warmup: int = DEFAULT_BENCHMARK_WARMUP,
) -> WorkspaceBenchmarkReport:
    """Benchmark workspace lookup across multiple repository catalogs.

    Args:
        repo_paths: Repository roots to search together.
        queries: Search queries to run.
        limit: Maximum combined hits requested per query.
        repeat: Number of measured iterations per query.
        warmup: Number of unmeasured warmup iterations per query.

    Returns:
        Workspace benchmark report with timing and aggregate savings.

    Raises:
        FileNotFoundError: If any supplied repository lacks a catalog.
        ValueError: If no repository or query is supplied.
    """
    if not repo_paths:
        raise ValueError("at least one repository path is required")
    benchmark_queries = _normalize_queries(queries)
    if not benchmark_queries:
        raise ValueError("No benchmark queries were provided.")

    repo_roots = [Path(repo_path).expanduser().resolve() for repo_path in repo_paths]
    stores = [CatalogStore.for_repo(repo_root, reuse_connection=True) for repo_root in repo_roots]
    for repo_root, store in zip(repo_roots, stores, strict=True):
        if not store.has_catalog():
            raise FileNotFoundError(
                f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first."
            )

    bounded_limit = max(1, min(limit, 100))
    bounded_repeat = max(1, repeat)
    bounded_warmup = max(0, warmup)
    health = [
        ProviderHealth(
            provider=repo_root.name if repo_root.name != "src" else f"{repo_root.parent.name}/src",
            available=True,
            details={
                "repo_path": str(repo_root),
                "database": str(store.database_path),
                "files": store.file_count(),
                "symbols": store.symbol_count(),
                "dependencies": store.dependency_count(),
                "text_lines": store.text_line_count(),
                "meta": store.get_meta(),
            },
        )
        for repo_root, store in zip(repo_roots, stores, strict=True)
    ]
    comparisons = [
        QueryComparison(
            query=query,
            runs=[
                _benchmark_workspace_query(
                    repo_roots,
                    stores,
                    query=query,
                    limit=bounded_limit,
                    repeat=bounded_repeat,
                    warmup=bounded_warmup,
                )
            ],
            overlap={},
        )
        for query in benchmark_queries
    ]
    return WorkspaceBenchmarkReport(
        repo_paths=[str(repo_root) for repo_root in repo_roots],
        mode="workspace-lookup",
        limit=bounded_limit,
        repeat=bounded_repeat,
        warmup=bounded_warmup,
        health=health,
        queries=comparisons,
    )


def run_workflow_benchmark(
    repo_paths: list[str | Path],
    *,
    queries: list[str],
    limit: int = 10,
    repeat: int = DEFAULT_BENCHMARK_REPEAT,
    warmup: int = DEFAULT_BENCHMARK_WARMUP,
    per_repo_limit: int | None = None,
    max_files: int = 5,
    context_lines: int = 4,
    max_lines_per_file: int = 80,
    include_tests: bool = True,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    fallback_text_limit: int | None = None,
) -> WorkflowBenchmarkReport:
    """Benchmark the agent workflow from query to compact source context.

    Args:
        repo_paths: Repository roots to search. A single repository uses the
            normal context-pack flow; multiple repositories use workspace
            context selection.
        queries: Search queries to run.
        limit: Maximum ranked lookup hits considered per query.
        repeat: Number of measured iterations per query.
        warmup: Number of unmeasured warmup iterations per query.
        per_repo_limit: Optional maximum hits requested per repository.
        max_files: Maximum distinct files to include snippets for.
        context_lines: Lines before and after selected hits.
        max_lines_per_file: Maximum source lines returned per selected file.
        include_tests: Whether test files may be selected for snippets.
        symbol_limit: Optional symbol lookup limit per repository.
        file_limit: Optional file path lookup limit per repository.
        text_limit: Optional text lookup limit per repository.
        fallback_text_limit: Optional text hit budget used only when text lookup
            is disabled and source/file lookup finds no hits.

    Returns:
        Workflow benchmark report with timing, payload size, selected context,
        and estimated avoided-token metrics.

    Raises:
        FileNotFoundError: If any supplied repository lacks a catalog.
        ValueError: If no repository or query is supplied.
    """
    if not repo_paths:
        raise ValueError("at least one repository path is required")
    benchmark_queries = _normalize_queries(queries)
    if not benchmark_queries:
        raise ValueError("No workflow benchmark queries were provided.")

    repo_roots = [Path(repo_path).expanduser().resolve() for repo_path in repo_paths]
    stores = [CatalogStore.for_repo(repo_root, reuse_connection=True) for repo_root in repo_roots]
    for repo_root, store in zip(repo_roots, stores, strict=True):
        if not store.has_catalog():
            raise FileNotFoundError(
                f"No catalog found at {store.database_path}. Run `code-intel scan {repo_root}` first."
            )

    bounded_limit = max(1, min(limit, 100))
    bounded_repeat = max(1, repeat)
    bounded_warmup = max(0, warmup)
    bounded_max_files = max(1, min(max_files, 20))
    bounded_context_lines = max(0, min(context_lines, 20))
    bounded_max_lines_per_file = max(1, min(max_lines_per_file, 500))

    queries_report = [
        _benchmark_workflow_query(
            repo_roots,
            stores,
            query=query,
            limit=bounded_limit,
            repeat=bounded_repeat,
            warmup=bounded_warmup,
            per_repo_limit=per_repo_limit,
            max_files=bounded_max_files,
            context_lines=bounded_context_lines,
            max_lines_per_file=bounded_max_lines_per_file,
            include_tests=include_tests,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            fallback_text_limit=fallback_text_limit,
        )
        for query in benchmark_queries
    ]
    return WorkflowBenchmarkReport(
        repo_paths=[str(repo_root) for repo_root in repo_roots],
        mode="context-pack" if len(repo_roots) == 1 else "workspace-context",
        limit=bounded_limit,
        repeat=bounded_repeat,
        warmup=bounded_warmup,
        per_repo_limit=per_repo_limit,
        max_files=bounded_max_files,
        context_lines=bounded_context_lines,
        max_lines_per_file=bounded_max_lines_per_file,
        include_tests=include_tests,
        health=[
            ProviderHealth(
                provider=_repo_label(repo_root),
                available=True,
                details={
                    "repo_path": str(repo_root),
                    "database": str(store.database_path),
                    "files": store.file_count(),
                    "symbols": store.symbol_count(),
                    "dependencies": store.dependency_count(),
                    "text_lines": store.text_line_count(),
                    "meta": store.get_meta(),
                },
            )
            for repo_root, store in zip(repo_roots, stores, strict=True)
        ],
        queries=queries_report,
    )


def report_to_dict(report: BenchmarkReport) -> dict[str, object]:
    """Serialize a benchmark report to plain dictionaries.

    Args:
        report: Benchmark report dataclass.

    Returns:
        JSON-serializable dictionary representation.
    """
    return asdict(report)


def report_to_summary_dict(report: BenchmarkReport) -> dict[str, object]:
    """Serialize a compact benchmark report for agent-facing comparisons.

    Args:
        report: Benchmark report dataclass.

    Returns:
        JSON-serializable benchmark summary without per-result payload detail.
    """
    return {
        "repo_path": report.repo_path,
        "mode": report.mode,
        "limit": report.limit,
        "repeat": report.repeat,
        "warmup": report.warmup,
        "providers": report.providers,
        "health": [_health_summary(health) for health in report.health],
        "queries": [_comparison_summary(comparison) for comparison in report.queries],
    }


def workspace_report_to_dict(report: WorkspaceBenchmarkReport) -> dict[str, object]:
    """Serialize a workspace benchmark report to plain dictionaries.

    Args:
        report: Workspace benchmark report dataclass.

    Returns:
        JSON-serializable dictionary representation.
    """
    return asdict(report)


def workspace_report_to_summary_dict(report: WorkspaceBenchmarkReport) -> dict[str, object]:
    """Serialize a compact workspace benchmark report.

    Args:
        report: Workspace benchmark report dataclass.

    Returns:
        JSON-serializable summary without per-result payload detail.
    """
    return {
        "repo_paths": report.repo_paths,
        "mode": report.mode,
        "limit": report.limit,
        "repeat": report.repeat,
        "warmup": report.warmup,
        "health": [_health_summary(health) for health in report.health],
        "queries": [_comparison_summary(comparison) for comparison in report.queries],
    }


def workflow_report_to_dict(report: WorkflowBenchmarkReport) -> dict[str, object]:
    """Serialize a workflow benchmark report to plain dictionaries.

    Args:
        report: Workflow benchmark report dataclass.

    Returns:
        JSON-serializable dictionary representation.
    """
    return asdict(report)


def workflow_report_to_summary_dict(report: WorkflowBenchmarkReport) -> dict[str, object]:
    """Serialize a compact workflow benchmark report.

    Args:
        report: Workflow benchmark report dataclass.

    Returns:
        JSON-serializable summary without per-hit detail.
    """
    return {
        "repo_paths": report.repo_paths,
        "mode": report.mode,
        "limit": report.limit,
        "repeat": report.repeat,
        "warmup": report.warmup,
        "per_repo_limit": report.per_repo_limit,
        "max_files": report.max_files,
        "context_lines": report.context_lines,
        "max_lines_per_file": report.max_lines_per_file,
        "include_tests": report.include_tests,
        "health": [_health_summary(health) for health in report.health],
        "queries": [_workflow_run_summary(run) for run in report.queries],
    }


def report_to_json(report: BenchmarkReport) -> str:
    """Serialize a benchmark report as formatted JSON.

    Args:
        report: Benchmark report dataclass.

    Returns:
        JSON string with deterministic key ordering.
    """
    return json.dumps(report_to_dict(report), indent=2, sort_keys=True)


def report_to_summary_json(report: BenchmarkReport) -> str:
    """Serialize a benchmark summary as formatted JSON.

    Args:
        report: Benchmark report dataclass.

    Returns:
        Compact JSON string with deterministic key ordering.
    """
    return json.dumps(report_to_summary_dict(report), indent=2, sort_keys=True)


def workspace_report_to_json(report: WorkspaceBenchmarkReport) -> str:
    """Serialize a workspace benchmark report as formatted JSON.

    Args:
        report: Workspace benchmark report dataclass.

    Returns:
        JSON string with deterministic key ordering.
    """
    return json.dumps(workspace_report_to_dict(report), indent=2, sort_keys=True)


def workspace_report_to_summary_json(report: WorkspaceBenchmarkReport) -> str:
    """Serialize a workspace benchmark summary as formatted JSON.

    Args:
        report: Workspace benchmark report dataclass.

    Returns:
        Compact JSON string with deterministic key ordering.
    """
    return json.dumps(workspace_report_to_summary_dict(report), indent=2, sort_keys=True)


def workflow_report_to_json(report: WorkflowBenchmarkReport) -> str:
    """Serialize a workflow benchmark report as formatted JSON.

    Args:
        report: Workflow benchmark report dataclass.

    Returns:
        JSON string with deterministic key ordering.
    """
    return json.dumps(workflow_report_to_dict(report), indent=2, sort_keys=True)


def workflow_report_to_summary_json(report: WorkflowBenchmarkReport) -> str:
    """Serialize a workflow benchmark summary as formatted JSON.

    Args:
        report: Workflow benchmark report dataclass.

    Returns:
        Compact JSON string with deterministic key ordering.
    """
    return json.dumps(workflow_report_to_summary_dict(report), indent=2, sort_keys=True)


def _health_summary(health: ProviderHealth) -> dict[str, object]:
    details = health.details
    meta = details.get("meta", {})
    generated_at = meta.get("generated_at", "") if isinstance(meta, dict) else ""
    indexed_at = details.get("indexed_at") or generated_at
    return {
        "provider": health.provider,
        "available": health.available,
        "files": details.get("files", 0),
        "symbols": details.get("symbols", 0),
        "indexed_at": indexed_at,
    }


def _comparison_summary(comparison: QueryComparison) -> dict[str, object]:
    return {
        "query": comparison.query,
        "runs": [_run_summary(run) for run in comparison.runs],
        "overlap": _overlap_summary(comparison.overlap),
    }


def _run_summary(run: QueryRun) -> dict[str, object]:
    return {
        "provider": run.provider,
        "mode": run.mode,
        "result_count": run.result_count,
        "median_ms": run.median_ms,
        "avg_ms": run.avg_ms,
        "min_ms": run.min_ms,
        "max_ms": run.max_ms,
        "candidate_files": run.candidate_files,
        "returned_files": run.returned_files,
        "estimated_saved_tokens": run.estimated_saved_tokens,
        "source_path_count": run.source_path_count,
        "test_path_count": run.test_path_count,
        "first_result_path": run.first_result_path,
        "first_result_kind": run.first_result_kind,
        "first_result_is_test": run.first_result_is_test,
        "top_paths": run.selected_paths[:5],
    }


def _workflow_run_summary(run: WorkflowRun) -> dict[str, object]:
    return {
        "query": run.query,
        "repeat": run.repeat,
        "median_ms": run.median_ms,
        "avg_ms": run.avg_ms,
        "min_ms": run.min_ms,
        "max_ms": run.max_ms,
        "hit_count": run.hit_count,
        "candidate_files": run.candidate_files,
        "selected_files": run.selected_files,
        "selected_lines": run.selected_lines,
        "estimated_tokens": run.estimated_tokens,
        "estimated_saved_tokens": run.estimated_saved_tokens,
        "payload_bytes": run.payload_bytes,
        "source_path_count": run.source_path_count,
        "test_path_count": run.test_path_count,
        "first_result_path": run.first_result_path,
        "first_result_kind": run.first_result_kind,
        "first_result_is_test": run.first_result_is_test,
        "top_paths": run.selected_paths[:5],
    }


def _overlap_summary(overlap: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for provider, value in overlap.items():
        if provider == "baseline":
            summary[provider] = value
            continue
        if not isinstance(value, dict):
            continue
        summary[provider] = {
            "shared_path_count": value.get("shared_path_count", 0),
            "jaccard": value.get("jaccard", 0.0),
        }
    return summary


def _benchmark_workspace_query(
    repo_roots: list[Path],
    stores: list[CatalogStore],
    *,
    query: str,
    limit: int,
    repeat: int,
    warmup: int,
) -> QueryRun:
    for _ in range(warmup):
        workspace_lookup(repo_roots, query, limit=limit, context_lines=0)

    timings_ms: list[float] = []
    result_rows: list[dict[str, object]] = []
    selected_paths_by_repo: dict[str, set[str]] = {}
    for _ in range(repeat):
        start = perf_counter()
        result = workspace_lookup(repo_roots, query, limit=limit, context_lines=0)
        timings_ms.append((perf_counter() - start) * 1000)
        payload = workspace_lookup_to_dict(result)
        result_rows = list(payload["hits"])
        selected_paths_by_repo = workspace_selected_paths_by_repo(result)

    savings = _workspace_savings(repo_roots, stores, selected_paths_by_repo, result_rows)
    selected_paths = _qualified_workspace_paths(selected_paths_by_repo)
    quality = _quality_metrics(selected_paths=selected_paths, rows=result_rows)
    return QueryRun(
        provider="workspace",
        mode="workspace-lookup",
        query=query,
        repeat=repeat,
        result_count=len(result_rows),
        avg_ms=round(sum(timings_ms) / len(timings_ms), 3),
        median_ms=round(median(timings_ms), 3),
        min_ms=round(min(timings_ms), 3),
        max_ms=round(max(timings_ms), 3),
        selected_paths=selected_paths,
        top_results=[_trim_result(row) for row in result_rows[:5]],
        estimated_saved_tokens=int(savings["estimated_saved_tokens"]),
        candidate_files=int(savings["candidate_files"]),
        returned_files=int(savings["returned_files"]),
        source_path_count=quality["source_path_count"],
        test_path_count=quality["test_path_count"],
        first_result_path=quality["first_result_path"],
        first_result_kind=quality["first_result_kind"],
        first_result_is_test=quality["first_result_is_test"],
    )


def _benchmark_workflow_query(
    repo_roots: list[Path],
    stores: list[CatalogStore],
    *,
    query: str,
    limit: int,
    repeat: int,
    warmup: int,
    per_repo_limit: int | None,
    max_files: int,
    context_lines: int,
    max_lines_per_file: int,
    include_tests: bool,
    symbol_limit: int | None,
    file_limit: int | None,
    text_limit: int | None,
    fallback_text_limit: int | None,
) -> WorkflowRun:
    for _ in range(warmup):
        _run_context_workflow(
            repo_roots,
            stores,
            query=query,
            limit=limit,
            per_repo_limit=per_repo_limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            include_tests=include_tests,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            fallback_text_limit=fallback_text_limit,
        )

    timings_ms: list[float] = []
    pack: ContextPack | None = None
    for _ in range(repeat):
        start = perf_counter()
        pack = _run_context_workflow(
            repo_roots,
            stores,
            query=query,
            limit=limit,
            per_repo_limit=per_repo_limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            include_tests=include_tests,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            fallback_text_limit=fallback_text_limit,
        )
        timings_ms.append((perf_counter() - start) * 1000)

    if pack is None:
        raise RuntimeError("workflow benchmark did not execute")

    selected_paths = _context_pack_paths(pack)
    quality = _quality_metrics(selected_paths=selected_paths, rows=_context_pack_top_hits(pack))
    return WorkflowRun(
        query=query,
        repeat=repeat,
        avg_ms=round(sum(timings_ms) / len(timings_ms), 3),
        median_ms=round(median(timings_ms), 3),
        min_ms=round(min(timings_ms), 3),
        max_ms=round(max(timings_ms), 3),
        hit_count=pack.hit_count,
        selected_files=pack.selected_files,
        selected_lines=pack.selected_lines,
        estimated_tokens=pack.estimated_tokens,
        estimated_saved_tokens=pack.estimated_saved_tokens,
        candidate_files=pack.candidate_files,
        payload_bytes=_json_byte_count(context_pack_to_dict(pack, compact=True, include_hits=False)),
        selected_paths=selected_paths,
        top_hits=_context_pack_top_hits(pack),
        source_path_count=quality["source_path_count"],
        test_path_count=quality["test_path_count"],
        first_result_path=quality["first_result_path"],
        first_result_kind=quality["first_result_kind"],
        first_result_is_test=quality["first_result_is_test"],
    )


def _benchmark_query(
    repo_root: Path,
    store: CatalogStore,
    query: str,
    *,
    providers: tuple[ProviderName, ...],
    mode: BenchmarkMode,
    limit: int,
    repeat: int,
    warmup: int,
) -> QueryComparison:
    runs = [
        _benchmark_provider(
            repo_root,
            store,
            provider,
            query=query,
            mode=mode,
            limit=limit,
            repeat=repeat,
            warmup=warmup,
        )
        for provider in providers
    ]
    return QueryComparison(query=query, runs=runs, overlap=_compute_overlap(runs))


def _benchmark_provider(
    repo_root: Path,
    store: CatalogStore,
    provider: ProviderName,
    *,
    query: str,
    mode: BenchmarkMode,
    limit: int,
    repeat: int,
    warmup: int,
) -> QueryRun:
    for _ in range(warmup):
        _run_search(repo_root, store, query, provider=provider, mode=mode, limit=limit)

    timings_ms: list[float] = []
    result_rows: list[dict[str, object]] = []
    selected_paths: set[str] = set()
    for _ in range(repeat):
        start = perf_counter()
        result_rows, selected_paths = _run_search(repo_root, store, query, provider=provider, mode=mode, limit=limit)
        timings_ms.append((perf_counter() - start) * 1000)

    sorted_paths = sorted(selected_paths)
    savings = estimate_saved_tokens_for_paths(store, set(sorted_paths), len(result_rows))
    quality = _quality_metrics(selected_paths=sorted_paths, rows=result_rows)
    return QueryRun(
        provider=provider,
        mode=mode,
        query=query,
        repeat=repeat,
        result_count=len(result_rows),
        avg_ms=round(sum(timings_ms) / len(timings_ms), 3),
        median_ms=round(median(timings_ms), 3),
        min_ms=round(min(timings_ms), 3),
        max_ms=round(max(timings_ms), 3),
        selected_paths=sorted_paths,
        top_results=[_trim_result(row) for row in result_rows[:5]],
        estimated_saved_tokens=int(savings["estimated_saved_tokens"]),
        candidate_files=int(savings["candidate_files"]),
        returned_files=int(savings["returned_files"]),
        source_path_count=quality["source_path_count"],
        test_path_count=quality["test_path_count"],
        first_result_path=quality["first_result_path"],
        first_result_kind=quality["first_result_kind"],
        first_result_is_test=quality["first_result_is_test"],
    )


def _provider_health(provider: ProviderName, repo_root: Path, store: CatalogStore) -> ProviderHealth:
    if provider == "catalog":
        return ProviderHealth(
            provider=provider,
            available=store.has_catalog(),
            details={
                "database": str(store.database_path),
                "files": store.file_count(),
                "symbols": store.symbol_count(),
                "dependencies": store.dependency_count(),
                "text_lines": store.text_line_count(),
                "meta": store.get_meta(),
            },
        )
    if provider == "jcodemunch":
        stats = get_jcodemunch_catalog_stats(repo_root)
        return ProviderHealth(
            provider=provider,
            available=bool(stats.get("database_path")),
            details=_compact_jcodemunch_health(stats),
        )
    return ProviderHealth(provider=provider, available=False, details={"error": f"Unsupported provider: {provider}"})


def _compact_jcodemunch_health(stats: dict[str, object]) -> dict[str, object]:
    details = {key: stats[key] for key in JCODEMUNCH_HEALTH_KEYS if key in stats}
    meta = stats.get("meta")
    if isinstance(meta, dict):
        details["meta"] = {key: meta[key] for key in JCODEMUNCH_META_SUMMARY_KEYS if key in meta}
        details["meta_key_count"] = len(meta)
        omitted_keys = sorted(set(meta) - set(JCODEMUNCH_META_SUMMARY_KEYS))
        if omitted_keys:
            details["omitted_meta_keys"] = omitted_keys
    error = stats.get("error")
    if error:
        details["error"] = error
    return details


def _compute_overlap(runs: list[QueryRun]) -> dict[str, object]:
    if len(runs) < 2:
        return {}
    baseline = runs[0]
    baseline_paths = set(baseline.selected_paths)
    comparisons: dict[str, object] = {"baseline": baseline.provider}
    for run in runs[1:]:
        run_paths = set(run.selected_paths)
        union = baseline_paths | run_paths
        intersection = baseline_paths & run_paths
        comparisons[run.provider] = {
            "shared_paths": sorted(intersection),
            "shared_path_count": len(intersection),
            "jaccard": round(len(intersection) / len(union), 3) if union else 1.0,
            "baseline_only": sorted(baseline_paths - run_paths),
            "provider_only": sorted(run_paths - baseline_paths),
        }
    return comparisons


def _normalize_queries(queries: list[str] | None) -> list[str]:
    if not queries:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for query in queries:
        stripped = query.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            normalized.append(stripped)
    return normalized


def _sample_catalog_queries(store: CatalogStore, limit: int) -> list[str]:
    rows = store.list_searchable_symbols(limit=max(20, limit))
    return [str(row["name"]) for row in rows[:limit]]


def _run_context_workflow(
    repo_roots: list[Path],
    stores: list[CatalogStore],
    *,
    query: str,
    limit: int,
    per_repo_limit: int | None,
    max_files: int,
    context_lines: int,
    max_lines_per_file: int,
    include_tests: bool,
    symbol_limit: int | None,
    file_limit: int | None,
    text_limit: int | None,
    fallback_text_limit: int | None,
) -> ContextPack:
    if len(repo_roots) == 1:
        return build_context_pack(
            repo_roots[0],
            stores[0],
            query,
            limit=limit,
            max_files=max_files,
            context_lines=context_lines,
            max_lines_per_file=max_lines_per_file,
            include_tests=include_tests,
            symbol_limit=symbol_limit,
            file_limit=file_limit,
            text_limit=text_limit,
            fallback_text_limit=fallback_text_limit,
        )
    return build_workspace_context_pack(
        repo_roots,
        query,
        limit=limit,
        per_repo_limit=per_repo_limit,
        max_files=max_files,
        context_lines=context_lines,
        max_lines_per_file=max_lines_per_file,
        include_tests=include_tests,
        symbol_limit=symbol_limit,
        file_limit=file_limit,
        text_limit=text_limit,
        fallback_text_limit=fallback_text_limit,
        stores_by_repo={str(repo_root): store for repo_root, store in zip(repo_roots, stores, strict=True)},
    )


def _normalize_mode(mode: str) -> BenchmarkMode:
    if mode in {"symbol", "text", "lookup"}:
        return mode
    raise ValueError(f"Unsupported benchmark mode: {mode}")


def _providers_for_mode(mode: BenchmarkMode, providers: tuple[ProviderName, ...]) -> tuple[ProviderName, ...]:
    if mode == "symbol":
        return providers
    return ("catalog",)


def _run_search(
    repo_root: Path,
    store: CatalogStore,
    query: str,
    *,
    provider: ProviderName,
    mode: BenchmarkMode,
    limit: int,
) -> tuple[list[dict[str, object]], set[str]]:
    if mode == "symbol":
        result = search_symbols(repo_root, store, query, limit=limit, provider=provider)
        return result.symbols, selected_paths_from_rows(result.symbols)
    if mode == "text":
        matches = store.search_text(query, limit=limit, context_lines=0)
        rows = [
            {
                "kind": "text",
                "path": match.path,
                "line": match.line,
                "label": match.content.strip(),
                "detail": match.content,
            }
            for match in matches
        ]
        return rows, selected_paths_from_text_matches(matches)
    if mode == "lookup":
        result = lookup(repo_root, store, query, limit=limit, context_lines=0)
        payload = lookup_to_dict(result)
        return list(payload["hits"]), lookup_selected_paths(result)
    raise ValueError(f"Unsupported benchmark mode: {mode}")


def _workspace_savings(
    repo_roots: list[Path],
    stores: list[CatalogStore],
    selected_paths_by_repo: dict[str, set[str]],
    result_rows: list[dict[str, object]],
) -> dict[str, int]:
    total = {"candidate_files": 0, "returned_files": 0, "estimated_saved_tokens": 0}
    hit_counts_by_repo: dict[str, int] = {}
    for row in result_rows:
        repo_path = str(row.get("repo_path", ""))
        hit_counts_by_repo[repo_path] = hit_counts_by_repo.get(repo_path, 0) + 1
    for repo_root, store in zip(repo_roots, stores, strict=True):
        repo_path = str(repo_root)
        paths = selected_paths_by_repo.get(repo_path, set())
        metrics = estimate_saved_tokens_for_paths(store, paths, hit_counts_by_repo.get(repo_path, 0))
        total["candidate_files"] += int(metrics["candidate_files"])
        total["returned_files"] += int(metrics["returned_files"])
        total["estimated_saved_tokens"] += int(metrics["estimated_saved_tokens"])
    return total


def _qualified_workspace_paths(selected_paths_by_repo: dict[str, set[str]]) -> list[str]:
    qualified: list[str] = []
    for repo_path, paths in selected_paths_by_repo.items():
        repo = Path(repo_path)
        label = repo.name if repo.name != "src" else f"{repo.parent.name}/src"
        qualified.extend(f"{label}:{path}" for path in sorted(paths))
    return sorted(qualified)


def _context_pack_paths(pack: ContextPack) -> list[str]:
    paths = {
        (f"{snippet.repo_label}:{snippet.path}" if len(pack.repo_paths) > 1 else snippet.path)
        for snippet in pack.snippets
    }
    return sorted(paths)


def _context_pack_top_hits(pack: ContextPack) -> list[dict[str, object]]:
    return [
        {
            "repo_label": hit.repo_label,
            "kind": hit.kind,
            "path": hit.path,
            "line": hit.line,
            "label": hit.label,
            "score": hit.score,
        }
        for hit in pack.hits[:5]
    ]


def _trim_result(row: dict[str, object]) -> dict[str, object]:
    return {
        "kind": row.get("kind", ""),
        "repo_label": row.get("repo_label", ""),
        "path": row.get("path", ""),
        "line": row.get("line", 0),
        "name": row.get("name", ""),
        "qualified_name": row.get("qualified_name", ""),
        "signature": row.get("signature", ""),
        "label": row.get("label", ""),
        "detail": row.get("detail", ""),
    }


def _quality_metrics(selected_paths: list[str], rows: list[dict[str, object]]) -> dict[str, object]:
    first = rows[0] if rows else {}
    first_path = _qualified_result_path(first)
    first_kind = str(first.get("kind", "")) if first else ""
    return {
        "source_path_count": sum(1 for path in selected_paths if not _is_test_path(path)),
        "test_path_count": sum(1 for path in selected_paths if _is_test_path(path)),
        "first_result_path": first_path,
        "first_result_kind": first_kind,
        "first_result_is_test": _is_test_path(first_path) if first_path else False,
    }


def _qualified_result_path(row: dict[str, object]) -> str:
    path = str(row.get("path", ""))
    repo_label = str(row.get("repo_label", ""))
    if repo_label and path and not path.startswith(f"{repo_label}:"):
        return f"{repo_label}:{path}"
    return path


def _is_test_path(path: str) -> bool:
    parsed = Path(path.split(":", 1)[-1])
    parts = set(parsed.parts)
    name = parsed.name
    return (
        bool(parts & {"test", "tests", "__tests__"})
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def _repo_label(repo_path: Path) -> str:
    parent = repo_path.parent.name
    name = repo_path.name
    if name == "src" and parent:
        return f"{parent}/{name}"
    return name


def _json_byte_count(payload: object) -> int:
    return len(json.dumps(payload, sort_keys=True).encode("utf-8"))
