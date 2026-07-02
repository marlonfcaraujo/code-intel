"""Persistent benchmark suites for repeatable real-world measurements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_intel.benchmark import (
    DEFAULT_BENCHMARK_PROVIDERS,
    BenchmarkReport,
    WorkflowBenchmarkReport,
    report_to_dict,
    report_to_summary_dict,
    run_benchmark,
    run_workflow_benchmark,
    workflow_report_to_dict,
    workflow_report_to_summary_dict,
)
from code_intel.symbol_search import ProviderName

BENCHMARK_SUITE_CONFIG_DIRNAME = "benchmark-suites"
BENCHMARK_SUITE_HISTORY_DIRNAME = "benchmark-runs"
DEFAULT_SUITE_LIMIT = 5
DEFAULT_SUITE_REPEAT = 5
DEFAULT_SUITE_WARMUP = 1
DEFAULT_SUITE_MAX_FILES = 3
DEFAULT_SUITE_CONTEXT_LINES = 3
DEFAULT_SUITE_MAX_LINES_PER_FILE = 40


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteConfig:
    """Named benchmark suite persisted as JSON.

    Attributes:
        name: Stable suite name.
        repos: Absolute repository roots included in workflow benchmarks.
        symbol_repo: Repository root used for provider comparison benchmarks.
        symbol_queries: Queries run through provider comparison in symbol mode.
        workflow_queries: Queries run through lookup-to-context workflow timing.
        path: Configuration file path backing the suite.
        workspace: Optional workspace name used when the suite was created.
        description: Optional human-readable suite description.
        limit: Default hit limit for suite runs.
        repeat: Default measured repetitions per query.
        warmup: Default warmup repetitions per query.
        per_repo_limit: Optional workflow hit limit requested per repository.
        max_files: Default maximum snippet files for workflow runs.
        context_lines: Default context lines before and after workflow hits.
        max_lines_per_file: Default maximum snippet lines per selected file.
        source_first: Whether workflow runs should use implementation-first
            lookup by default.
    """

    name: str
    repos: list[str]
    symbol_repo: str
    symbol_queries: list[str]
    workflow_queries: list[str]
    path: str
    workspace: str = ""
    description: str = ""
    limit: int = DEFAULT_SUITE_LIMIT
    repeat: int = DEFAULT_SUITE_REPEAT
    warmup: int = DEFAULT_SUITE_WARMUP
    per_repo_limit: int | None = None
    max_files: int = DEFAULT_SUITE_MAX_FILES
    context_lines: int = DEFAULT_SUITE_CONTEXT_LINES
    max_lines_per_file: int = DEFAULT_SUITE_MAX_LINES_PER_FILE
    source_first: bool = True


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteRun:
    """Result from running a persisted benchmark suite.

    Attributes:
        config: Suite configuration used for the run.
        providers: Symbol benchmark providers used for provider comparisons.
        symbol_report: Provider comparison report, when symbol queries exist.
        workflow_report: Workflow benchmark report, when workflow queries exist.
    """

    config: BenchmarkSuiteConfig
    providers: list[str]
    symbol_report: BenchmarkReport | None
    workflow_report: WorkflowBenchmarkReport | None


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteHistoryRecord:
    """Stored benchmark suite scorecard with optional previous-run deltas.

    Attributes:
        path: JSONL history file path that stores this record.
        entry: JSON-serializable history entry that was stored.
        previous_entry: Previous history entry for the same suite, when one
            existed before this record was appended.
        delta: Scorecard deltas versus the previous entry, or ``None`` when
            this is the first stored run for the suite.
    """

    path: str
    entry: dict[str, Any]
    previous_entry: dict[str, Any] | None
    delta: dict[str, Any] | None


def default_benchmark_suite_config_dir() -> Path:
    """Return the default benchmark-suite configuration directory.

    Returns:
        Path under the user's home directory for benchmark suite JSON files.
    """
    return Path.home() / ".code-intel" / BENCHMARK_SUITE_CONFIG_DIRNAME


def default_benchmark_suite_history_dir() -> Path:
    """Return the default benchmark-suite history directory.

    Returns:
        Path under the user's home directory for benchmark suite run JSONL
        history files.
    """
    return Path.home() / ".code-intel" / BENCHMARK_SUITE_HISTORY_DIRNAME


def save_benchmark_suite_config(
    name: str,
    repos: list[str | Path],
    *,
    symbol_queries: list[str] | None = None,
    workflow_queries: list[str] | None = None,
    symbol_repo: str | Path | None = None,
    workspace: str = "",
    description: str = "",
    config_dir: str | Path | None = None,
    limit: int = DEFAULT_SUITE_LIMIT,
    repeat: int = DEFAULT_SUITE_REPEAT,
    warmup: int = DEFAULT_SUITE_WARMUP,
    per_repo_limit: int | None = None,
    max_files: int = DEFAULT_SUITE_MAX_FILES,
    context_lines: int = DEFAULT_SUITE_CONTEXT_LINES,
    max_lines_per_file: int = DEFAULT_SUITE_MAX_LINES_PER_FILE,
    source_first: bool = True,
) -> BenchmarkSuiteConfig:
    """Persist a named benchmark suite.

    Args:
        name: Suite name. Names may contain letters, numbers, ``.``, ``-``,
            and ``_``.
        repos: Repository roots included in workflow benchmarks.
        symbol_queries: Provider-comparison symbol queries.
        workflow_queries: Lookup-to-context workflow queries.
        symbol_repo: Repository root for symbol provider comparisons. Defaults
            to the first repository.
        workspace: Optional workspace name used to create this suite.
        description: Optional suite description.
        config_dir: Optional directory for suite JSON files.
        limit: Default hit limit for suite runs.
        repeat: Default measured repetitions per query.
        warmup: Default unmeasured warmup repetitions per query.
        per_repo_limit: Optional workflow hit limit per repository.
        max_files: Maximum snippet files for workflow runs.
        context_lines: Context lines before and after workflow hits.
        max_lines_per_file: Maximum snippet lines per selected file.
        source_first: Whether workflow runs default to implementation-first
            lookup.

    Returns:
        Saved benchmark suite configuration.

    Raises:
        ValueError: If the name, repos, queries, or symbol repository are
            invalid.
        FileNotFoundError: If a repository path does not exist.
        NotADirectoryError: If a repository path is not a directory.
    """
    suite_name = normalize_benchmark_suite_name(name)
    repo_paths = _resolve_repo_paths(repos)
    if not repo_paths:
        raise ValueError("at least one repository path is required")
    normalized_symbol_queries = _normalize_queries(symbol_queries)
    normalized_workflow_queries = _normalize_queries(workflow_queries)
    if not normalized_symbol_queries and not normalized_workflow_queries:
        raise ValueError("at least one symbol or workflow query is required")

    resolved_symbol_repo = _resolve_symbol_repo(symbol_repo or repo_paths[0], repo_paths)
    directory = _benchmark_suite_config_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = benchmark_suite_config_path(suite_name, config_dir=directory)
    config = BenchmarkSuiteConfig(
        name=suite_name,
        repos=[str(repo_path) for repo_path in repo_paths],
        symbol_repo=str(resolved_symbol_repo),
        symbol_queries=normalized_symbol_queries,
        workflow_queries=normalized_workflow_queries,
        path=str(path),
        workspace=workspace.strip(),
        description=description.strip(),
        limit=max(1, min(limit, 100)),
        repeat=max(1, repeat),
        warmup=max(0, warmup),
        per_repo_limit=_bounded_optional_int(per_repo_limit),
        max_files=max(1, min(max_files, 20)),
        context_lines=max(0, min(context_lines, 20)),
        max_lines_per_file=max(1, min(max_lines_per_file, 500)),
        source_first=source_first,
    )
    path.write_text(json.dumps(_config_payload(config), indent=2, sort_keys=True) + "\n")
    return config


def load_benchmark_suite_config(
    name: str,
    *,
    config_dir: str | Path | None = None,
) -> BenchmarkSuiteConfig:
    """Load a named benchmark suite.

    Args:
        name: Suite name.
        config_dir: Optional directory for suite JSON files.

    Returns:
        Benchmark suite configuration.

    Raises:
        FileNotFoundError: If the named suite file does not exist.
        ValueError: If the suite file is malformed.
    """
    suite_name = normalize_benchmark_suite_name(name)
    path = benchmark_suite_config_path(suite_name, config_dir=config_dir)
    if not path.exists():
        raise FileNotFoundError(f"Benchmark suite is not configured: {suite_name} ({path})")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Benchmark suite configuration is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Benchmark suite configuration must be a JSON object: {path}")
    return _config_from_payload(payload, path=path)


def list_benchmark_suite_configs(*, config_dir: str | Path | None = None) -> list[BenchmarkSuiteConfig]:
    """List configured benchmark suites.

    Args:
        config_dir: Optional directory for suite JSON files.

    Returns:
        Benchmark suite configurations sorted by name.
    """
    directory = _benchmark_suite_config_dir(config_dir)
    if not directory.exists():
        return []
    configs = [load_benchmark_suite_config(path.stem, config_dir=directory) for path in directory.glob("*.json")]
    return sorted(configs, key=lambda config: config.name)


def benchmark_suite_config_path(name: str, *, config_dir: str | Path | None = None) -> Path:
    """Return the file path for a named benchmark suite.

    Args:
        name: Suite name.
        config_dir: Optional directory for suite JSON files.

    Returns:
        Path to the suite JSON file.
    """
    suite_name = normalize_benchmark_suite_name(name)
    return _benchmark_suite_config_dir(config_dir) / f"{suite_name}.json"


def benchmark_suite_history_path(name: str, *, history_dir: str | Path | None = None) -> Path:
    """Return the JSONL history path for a named benchmark suite.

    Args:
        name: Suite name.
        history_dir: Optional directory for suite run history files.

    Returns:
        Path to the suite run history JSONL file.
    """
    suite_name = normalize_benchmark_suite_name(name)
    return _benchmark_suite_history_dir(history_dir) / f"{suite_name}.jsonl"


def benchmark_suite_config_to_dict(config: BenchmarkSuiteConfig) -> dict[str, Any]:
    """Serialize a benchmark suite configuration.

    Args:
        config: Benchmark suite configuration.

    Returns:
        JSON-serializable dictionary including the backing path.
    """
    payload = _config_payload(config)
    payload["path"] = config.path
    return payload


def run_benchmark_suite(
    config: BenchmarkSuiteConfig,
    *,
    providers: tuple[ProviderName, ...] = DEFAULT_BENCHMARK_PROVIDERS,
    limit: int | None = None,
    repeat: int | None = None,
    warmup: int | None = None,
) -> BenchmarkSuiteRun:
    """Run a persisted benchmark suite.

    Args:
        config: Benchmark suite configuration.
        providers: Symbol providers for provider-comparison queries.
        limit: Optional hit-limit override.
        repeat: Optional measured-repetition override.
        warmup: Optional warmup-repetition override.

    Returns:
        Benchmark suite run containing symbol and workflow reports.

    Raises:
        FileNotFoundError: If any required catalog is missing.
        ValueError: If configured queries are invalid.
    """
    run_limit = max(1, min(limit if limit is not None else config.limit, 100))
    run_repeat = max(1, repeat if repeat is not None else config.repeat)
    run_warmup = max(0, warmup if warmup is not None else config.warmup)
    symbol_report = (
        run_benchmark(
            config.symbol_repo,
            queries=config.symbol_queries,
            providers=providers,
            mode="symbol",
            limit=run_limit,
            repeat=run_repeat,
            warmup=run_warmup,
        )
        if config.symbol_queries
        else None
    )
    workflow_report = (
        run_workflow_benchmark(
            config.repos,
            queries=config.workflow_queries,
            limit=run_limit,
            repeat=run_repeat,
            warmup=run_warmup,
            per_repo_limit=config.per_repo_limit,
            max_files=config.max_files,
            context_lines=config.context_lines,
            max_lines_per_file=config.max_lines_per_file,
            include_tests=not config.source_first,
            text_limit=0 if config.source_first else None,
            fallback_text_limit=run_limit if config.source_first else None,
        )
        if config.workflow_queries
        else None
    )
    return BenchmarkSuiteRun(
        config=config,
        providers=list(providers),
        symbol_report=symbol_report,
        workflow_report=workflow_report,
    )


def benchmark_suite_run_to_dict(run: BenchmarkSuiteRun, *, summary: bool = True) -> dict[str, Any]:
    """Serialize a benchmark suite run.

    Args:
        run: Benchmark suite run to serialize.
        summary: Whether to use compact benchmark summaries.

    Returns:
        JSON-serializable suite run payload.
    """
    symbol_report = None
    if run.symbol_report is not None:
        symbol_report = report_to_summary_dict(run.symbol_report) if summary else report_to_dict(run.symbol_report)
    workflow_report = None
    if run.workflow_report is not None:
        workflow_report = (
            workflow_report_to_summary_dict(run.workflow_report)
            if summary
            else workflow_report_to_dict(run.workflow_report)
        )
    return {
        "suite": benchmark_suite_config_to_dict(run.config),
        "providers": run.providers,
        "scorecard": benchmark_suite_scorecard(run),
        "symbol": symbol_report,
        "workflow": workflow_report,
    }


def benchmark_suite_scorecard(run: BenchmarkSuiteRun) -> dict[str, Any]:
    """Build compact quality and speed metrics for a suite run.

    Args:
        run: Benchmark suite run to summarize.

    Returns:
        Scorecard with provider quality counts, provider speed ratios, workflow
        first-result quality, and aggregate token/context metrics.
    """
    return {
        "symbol": _symbol_scorecard(run.symbol_report),
        "workflow": _workflow_scorecard(run.workflow_report),
    }


def record_benchmark_suite_run(
    run: BenchmarkSuiteRun,
    *,
    history_dir: str | Path | None = None,
) -> BenchmarkSuiteHistoryRecord:
    """Append a benchmark suite scorecard to persistent history.

    Args:
        run: Benchmark suite run to record.
        history_dir: Optional directory for suite run history files.

    Returns:
        Stored history record including deltas versus the previous run.
    """
    path = benchmark_suite_history_path(run.config.name, history_dir=history_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_benchmark_suite_history(run.config.name, history_dir=history_dir)
    previous = existing[-1] if existing else None
    scorecard = benchmark_suite_scorecard(run)
    previous_scorecard = previous.get("scorecard", {}) if previous else {}
    previous_scorecard = previous_scorecard if isinstance(previous_scorecard, dict) else {}
    delta = benchmark_suite_scorecard_delta(scorecard, previous_scorecard) if previous else None
    entry = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "suite_name": run.config.name,
        "suite_path": run.config.path,
        "providers": run.providers,
        "scorecard": scorecard,
        "delta": delta,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return BenchmarkSuiteHistoryRecord(
        path=str(path),
        entry=entry,
        previous_entry=previous,
        delta=delta,
    )


def load_benchmark_suite_history(
    name: str,
    *,
    history_dir: str | Path | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load benchmark suite scorecard history.

    Args:
        name: Suite name.
        history_dir: Optional directory for suite run history files.
        limit: Optional maximum number of most-recent records to return.

    Returns:
        History entries ordered from oldest to newest.

    Raises:
        ValueError: If a history line is not valid JSON.
    """
    path = benchmark_suite_history_path(name, history_dir=history_dir)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Benchmark suite history is not valid JSON: {path}:{line_number}") from exc
        if isinstance(payload, dict):
            entries.append(payload)
    if limit is None:
        return entries
    bounded_limit = max(1, limit)
    return entries[-bounded_limit:]


def benchmark_suite_scorecard_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Compare two benchmark suite scorecards.

    Args:
        current: Current suite scorecard.
        previous: Previous suite scorecard.

    Returns:
        Delta payload where positive latency/token values mean the current run
        increased and positive source-first values mean quality improved.
    """
    return {
        "symbol": _symbol_scorecard_delta(_dict_value(current, "symbol"), _dict_value(previous, "symbol")),
        "workflow": _workflow_scorecard_delta(_dict_value(current, "workflow"), _dict_value(previous, "workflow")),
    }


def normalize_benchmark_suite_name(name: str) -> str:
    """Validate and normalize a benchmark suite name.

    Args:
        name: User-supplied suite name.

    Returns:
        Stripped suite name.

    Raises:
        ValueError: If the name is empty or contains unsafe path characters.
    """
    normalized = name.strip()
    if not normalized:
        raise ValueError("benchmark suite name must not be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(character not in allowed for character in normalized):
        raise ValueError("benchmark suite name may only contain letters, numbers, '.', '-', and '_'")
    return normalized


def _config_from_payload(payload: dict[str, Any], *, path: Path) -> BenchmarkSuiteConfig:
    name = payload.get("name")
    repos = payload.get("repos")
    symbol_repo = payload.get("symbol_repo")
    symbol_queries = payload.get("symbol_queries", [])
    workflow_queries = payload.get("workflow_queries", [])
    if not isinstance(name, str):
        raise ValueError(f"Benchmark suite name must be a string: {path}")
    if not isinstance(repos, list) or not repos or not all(isinstance(repo, str) for repo in repos):
        raise ValueError(f"Benchmark suite repos must be a non-empty string list: {path}")
    if not isinstance(symbol_repo, str) or not symbol_repo:
        raise ValueError(f"Benchmark suite symbol_repo must be a string: {path}")
    if not isinstance(symbol_queries, list) or not all(isinstance(query, str) for query in symbol_queries):
        raise ValueError(f"Benchmark suite symbol_queries must be a string list: {path}")
    if not isinstance(workflow_queries, list) or not all(isinstance(query, str) for query in workflow_queries):
        raise ValueError(f"Benchmark suite workflow_queries must be a string list: {path}")
    normalized_symbol_queries = _normalize_queries(symbol_queries)
    normalized_workflow_queries = _normalize_queries(workflow_queries)
    if not normalized_symbol_queries and not normalized_workflow_queries:
        raise ValueError(f"Benchmark suite must contain at least one query: {path}")
    return BenchmarkSuiteConfig(
        name=normalize_benchmark_suite_name(name),
        repos=[str(Path(repo).expanduser().resolve()) for repo in repos],
        symbol_repo=str(Path(symbol_repo).expanduser().resolve()),
        symbol_queries=normalized_symbol_queries,
        workflow_queries=normalized_workflow_queries,
        path=str(path),
        workspace=str(payload.get("workspace", "") or ""),
        description=str(payload.get("description", "") or ""),
        limit=_bounded_int(payload.get("limit"), default=DEFAULT_SUITE_LIMIT, upper=100),
        repeat=_bounded_int(payload.get("repeat"), default=DEFAULT_SUITE_REPEAT),
        warmup=max(0, int(payload.get("warmup", DEFAULT_SUITE_WARMUP) or 0)),
        per_repo_limit=_bounded_optional_int(payload.get("per_repo_limit")),
        max_files=_bounded_int(payload.get("max_files"), default=DEFAULT_SUITE_MAX_FILES, upper=20),
        context_lines=_bounded_int(
            payload.get("context_lines"), default=DEFAULT_SUITE_CONTEXT_LINES, lower=0, upper=20
        ),
        max_lines_per_file=_bounded_int(
            payload.get("max_lines_per_file"),
            default=DEFAULT_SUITE_MAX_LINES_PER_FILE,
            upper=500,
        ),
        source_first=bool(payload.get("source_first", True)),
    )


def _symbol_scorecard(report: BenchmarkReport | None) -> dict[str, Any]:
    if report is None:
        return {
            "query_count": 0,
            "providers": {},
            "comparisons": [],
        }

    provider_stats: dict[str, dict[str, Any]] = {}
    for comparison in report.queries:
        for run in comparison.runs:
            stats = provider_stats.setdefault(
                run.provider,
                {
                    "query_count": 0,
                    "median_ms_total": 0.0,
                    "result_count_total": 0,
                    "source_path_total": 0,
                    "test_path_total": 0,
                    "source_first_count": 0,
                    "test_first_count": 0,
                    "first_paths": [],
                },
            )
            stats["query_count"] += 1
            stats["median_ms_total"] += run.median_ms
            stats["result_count_total"] += run.result_count
            stats["source_path_total"] += run.source_path_count
            stats["test_path_total"] += run.test_path_count
            if run.first_result_path:
                stats["first_paths"].append(run.first_result_path)
                if run.first_result_is_test:
                    stats["test_first_count"] += 1
                else:
                    stats["source_first_count"] += 1

    providers = {provider: _finalize_provider_stats(stats) for provider, stats in sorted(provider_stats.items())}
    return {
        "query_count": len(report.queries),
        "providers": providers,
        "comparisons": _provider_comparisons(
            providers, baseline_provider=report.providers[0] if report.providers else ""
        ),
    }


def _workflow_scorecard(report: WorkflowBenchmarkReport | None) -> dict[str, Any]:
    if report is None:
        return {
            "query_count": 0,
            "source_first_count": 0,
            "test_first_count": 0,
            "source_path_total": 0,
            "test_path_total": 0,
            "selected_files_total": 0,
            "selected_lines_total": 0,
            "estimated_tokens_total": 0,
            "estimated_saved_tokens_total": 0,
            "payload_bytes_total": 0,
            "median_ms_avg": 0.0,
            "first_paths": [],
        }

    median_ms_total = sum(run.median_ms for run in report.queries)
    query_count = len(report.queries)
    return {
        "query_count": query_count,
        "source_first_count": sum(
            1 for run in report.queries if run.first_result_path and not run.first_result_is_test
        ),
        "test_first_count": sum(1 for run in report.queries if run.first_result_is_test),
        "source_path_total": sum(run.source_path_count for run in report.queries),
        "test_path_total": sum(run.test_path_count for run in report.queries),
        "selected_files_total": sum(run.selected_files for run in report.queries),
        "selected_lines_total": sum(run.selected_lines for run in report.queries),
        "estimated_tokens_total": sum(run.estimated_tokens for run in report.queries),
        "estimated_saved_tokens_total": sum(run.estimated_saved_tokens for run in report.queries),
        "payload_bytes_total": sum(run.payload_bytes for run in report.queries),
        "median_ms_avg": round(median_ms_total / query_count, 3) if query_count else 0.0,
        "first_paths": [run.first_result_path for run in report.queries if run.first_result_path],
    }


def _finalize_provider_stats(stats: dict[str, Any]) -> dict[str, Any]:
    query_count = int(stats["query_count"])
    return {
        "query_count": query_count,
        "median_ms_avg": round(float(stats["median_ms_total"]) / query_count, 3) if query_count else 0.0,
        "result_count_total": int(stats["result_count_total"]),
        "source_path_total": int(stats["source_path_total"]),
        "test_path_total": int(stats["test_path_total"]),
        "source_first_count": int(stats["source_first_count"]),
        "test_first_count": int(stats["test_first_count"]),
        "first_paths": list(stats["first_paths"]),
    }


def _provider_comparisons(providers: dict[str, dict[str, Any]], *, baseline_provider: str) -> list[dict[str, Any]]:
    if not baseline_provider or baseline_provider not in providers:
        return []
    baseline = providers[baseline_provider]
    baseline_median = float(baseline["median_ms_avg"])
    comparisons: list[dict[str, Any]] = []
    for provider, stats in providers.items():
        if provider == baseline_provider:
            continue
        provider_median = float(stats["median_ms_avg"])
        comparisons.append(
            {
                "baseline": baseline_provider,
                "provider": provider,
                "baseline_median_ms_avg": baseline_median,
                "provider_median_ms_avg": provider_median,
                "baseline_speedup": round(provider_median / baseline_median, 3) if baseline_median > 0 else 0.0,
                "baseline_source_first_delta": int(baseline["source_first_count"]) - int(stats["source_first_count"]),
                "provider_test_first_delta": int(stats["test_first_count"]) - int(baseline["test_first_count"]),
            }
        )
    return comparisons


def _symbol_scorecard_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    current_providers = _dict_value(current, "providers")
    previous_providers = _dict_value(previous, "providers")
    provider_deltas: dict[str, dict[str, Any]] = {}
    for provider, current_stats in current_providers.items():
        if not isinstance(current_stats, dict):
            continue
        previous_stats = previous_providers.get(provider, {})
        previous_stats = previous_stats if isinstance(previous_stats, dict) else {}
        provider_deltas[provider] = {
            "median_ms_avg_delta": _float_delta(current_stats, previous_stats, "median_ms_avg"),
            "source_first_delta": _int_delta(current_stats, previous_stats, "source_first_count"),
            "test_first_delta": _int_delta(current_stats, previous_stats, "test_first_count"),
            "source_path_total_delta": _int_delta(current_stats, previous_stats, "source_path_total"),
            "test_path_total_delta": _int_delta(current_stats, previous_stats, "test_path_total"),
        }
    return {
        "query_count_delta": _int_delta(current, previous, "query_count"),
        "providers": provider_deltas,
        "comparisons": _comparison_deltas(current.get("comparisons", []), previous.get("comparisons", [])),
    }


def _workflow_scorecard_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_count_delta": _int_delta(current, previous, "query_count"),
        "median_ms_avg_delta": _float_delta(current, previous, "median_ms_avg"),
        "source_first_delta": _int_delta(current, previous, "source_first_count"),
        "test_first_delta": _int_delta(current, previous, "test_first_count"),
        "source_path_total_delta": _int_delta(current, previous, "source_path_total"),
        "test_path_total_delta": _int_delta(current, previous, "test_path_total"),
        "selected_files_total_delta": _int_delta(current, previous, "selected_files_total"),
        "selected_lines_total_delta": _int_delta(current, previous, "selected_lines_total"),
        "estimated_tokens_total_delta": _int_delta(current, previous, "estimated_tokens_total"),
        "estimated_saved_tokens_total_delta": _int_delta(current, previous, "estimated_saved_tokens_total"),
        "payload_bytes_total_delta": _int_delta(current, previous, "payload_bytes_total"),
    }


def _comparison_deltas(current: object, previous: object) -> list[dict[str, Any]]:
    if not isinstance(current, list):
        return []
    previous_by_provider: dict[str, dict[str, Any]] = {}
    if isinstance(previous, list):
        for row in previous:
            if isinstance(row, dict):
                previous_by_provider[str(row.get("provider", ""))] = row
    deltas: list[dict[str, Any]] = []
    for row in current:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider", ""))
        previous_row = previous_by_provider.get(provider, {})
        deltas.append(
            {
                "baseline": row.get("baseline", ""),
                "provider": provider,
                "baseline_speedup_delta": _float_delta(row, previous_row, "baseline_speedup"),
                "baseline_source_first_delta_delta": _int_delta(row, previous_row, "baseline_source_first_delta"),
                "provider_test_first_delta_delta": _int_delta(row, previous_row, "provider_test_first_delta"),
            }
        )
    return deltas


def _dict_value(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return value if isinstance(value, dict) else {}


def _int_delta(current: dict[str, Any], previous: dict[str, Any], key: str) -> int:
    return int(current.get(key, 0) or 0) - int(previous.get(key, 0) or 0)


def _float_delta(current: dict[str, Any], previous: dict[str, Any], key: str) -> float:
    return round(float(current.get(key, 0.0) or 0.0) - float(previous.get(key, 0.0) or 0.0), 3)


def _config_payload(config: BenchmarkSuiteConfig) -> dict[str, Any]:
    return {
        "name": config.name,
        "workspace": config.workspace,
        "description": config.description,
        "repos": config.repos,
        "symbol_repo": config.symbol_repo,
        "symbol_queries": config.symbol_queries,
        "workflow_queries": config.workflow_queries,
        "limit": config.limit,
        "repeat": config.repeat,
        "warmup": config.warmup,
        "per_repo_limit": config.per_repo_limit,
        "max_files": config.max_files,
        "context_lines": config.context_lines,
        "max_lines_per_file": config.max_lines_per_file,
        "source_first": config.source_first,
    }


def _benchmark_suite_config_dir(config_dir: str | Path | None) -> Path:
    return Path(config_dir).expanduser().resolve() if config_dir else default_benchmark_suite_config_dir()


def _benchmark_suite_history_dir(history_dir: str | Path | None) -> Path:
    return Path(history_dir).expanduser().resolve() if history_dir else default_benchmark_suite_history_dir()


def _resolve_repo_paths(repos: list[str | Path]) -> list[Path]:
    resolved: list[Path] = []
    for repo in repos:
        path = Path(repo).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {path}")
        resolved.append(path)
    return resolved


def _resolve_symbol_repo(symbol_repo: str | Path, repos: list[Path]) -> Path:
    path = Path(symbol_repo).expanduser().resolve()
    if path not in repos:
        raise ValueError("symbol repository must be one of the suite repositories")
    return path


def _normalize_queries(queries: list[str] | None) -> list[str]:
    if not queries:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for query in queries:
        stripped = query.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def _bounded_int(value: object, *, default: int, lower: int = 1, upper: int | None = None) -> int:
    try:
        bounded = int(value) if value is not None else default
    except (TypeError, ValueError):
        bounded = default
    bounded = max(lower, bounded)
    return min(bounded, upper) if upper is not None else bounded


def _bounded_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        bounded = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(bounded, 100))
