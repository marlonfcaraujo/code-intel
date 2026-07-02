"""Command line interface for code-intel."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from code_intel.agent_notes import install_agent_notes
from code_intel.benchmark import (
    DEFAULT_BENCHMARK_PROVIDERS,
    report_to_json,
    report_to_summary_json,
    run_benchmark,
    run_workflow_benchmark,
    run_workspace_benchmark,
    workflow_report_to_json,
    workflow_report_to_summary_json,
    workspace_report_to_json,
    workspace_report_to_summary_json,
)
from code_intel.benchmark_suite import (
    BenchmarkSuiteHistoryRecord,
    BenchmarkSuiteRun,
    benchmark_suite_config_to_dict,
    benchmark_suite_run_to_dict,
    benchmark_suite_scorecard,
    list_benchmark_suite_configs,
    load_benchmark_suite_config,
    load_benchmark_suite_history,
    record_benchmark_suite_run,
    run_benchmark_suite,
    save_benchmark_suite_config,
)
from code_intel.catalog_store import DEFAULT_CATALOG_PATH, CatalogStore
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
from code_intel.mcp_server import serve_mcp
from code_intel.references import (
    ReferenceFileSummary,
    ReferenceMatch,
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
from code_intel.refresh_job import (
    DEFAULT_REFRESH_INTERVAL_MINUTES,
    DEFAULT_REFRESH_LABEL,
    build_launchd_refresh_job,
    write_launchd_refresh_job,
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
    save_workspace_config,
    workspace_config_to_dict,
)
from code_intel.workspace_scan import scan_workspace, workspace_scan_to_dict


def main(argv: list[str] | None = None) -> int:
    """Run the code-intel CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except Exception as exc:  # pragma: no cover - top-level safety
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-intel", description="Repository intelligence CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Catalog a repository")
    scan_parser.add_argument("repo", nargs="?", default=".", help="Repository path")
    scan_parser.add_argument("--db", help=f"Database path (default: <repo>/{DEFAULT_CATALOG_PATH})")
    scan_parser.add_argument("--incremental", action="store_true", help="Reuse unchanged file analyses when possible")
    scan_parser.add_argument(
        "--skip-unchanged-meta",
        action="store_true",
        help="Skip metadata-only writes when an incremental scan finds no file changes",
    )
    scan_parser.add_argument("--workers", type=int, help="Analysis worker threads; use 1 for serial profiling")
    scan_parser.add_argument("--json", action="store_true", help="Emit JSON")
    scan_parser.set_defaults(func=_cmd_scan)

    find_parser = subparsers.add_parser("find", help="Search cataloged symbols")
    _add_repo_and_db_args(find_parser)
    find_parser.add_argument("query", help="Symbol query")
    find_parser.add_argument("--limit", type=int, default=20, help="Maximum results")
    find_parser.add_argument(
        "--provider",
        choices=("catalog", "jcodemunch"),
        default="catalog",
        help="Symbol provider to use (default: catalog)",
    )
    find_parser.set_defaults(func=_cmd_find)

    text_parser = subparsers.add_parser("search-text", help="Search indexed source text with bounded snippets")
    _add_repo_and_db_args(text_parser)
    text_parser.add_argument("query", help="Text fragment to search")
    text_parser.add_argument("--limit", type=int, default=20, help="Maximum matches")
    text_parser.add_argument("--context", type=int, default=1, help="Context lines before and after each match")
    text_parser.add_argument("--json", action="store_true", help="Emit JSON")
    text_parser.set_defaults(func=_cmd_search_text)

    references_parser = subparsers.add_parser("references", help="Find exact source references for an identifier")
    _add_repo_and_db_args(references_parser)
    references_parser.add_argument("query", help="Identifier or text fragment to find")
    references_parser.add_argument("--limit", type=int, default=50, help="Maximum exact references")
    references_parser.add_argument("--context", type=int, default=1, help="Context lines before and after each match")
    references_parser.add_argument(
        "--no-definitions", action="store_true", help="Exclude definition lines from results"
    )
    references_parser.add_argument("--ignore-case", action="store_true", help="Ignore case when filtering matches")
    references_parser.add_argument(
        "--summary-only", action="store_true", help="Return one compact row per matched file"
    )
    references_parser.add_argument("--json", action="store_true", help="Emit JSON")
    references_parser.set_defaults(func=_cmd_references)

    lookup_parser = subparsers.add_parser("lookup", help="Search symbols and source text together")
    _add_repo_and_db_args(lookup_parser)
    lookup_parser.add_argument("query", help="Symbol or text query")
    lookup_parser.add_argument("--limit", type=int, default=20, help="Maximum combined hits")
    lookup_parser.add_argument("--symbol-limit", type=int, help="Maximum symbol hits to consider")
    lookup_parser.add_argument("--file-limit", type=int, help="Maximum file path hits to consider")
    lookup_parser.add_argument("--text-limit", type=int, help="Maximum text hits to consider; use 0 to skip text")
    lookup_parser.add_argument("--context", type=int, default=0, help="Context lines for text hits")
    lookup_parser.add_argument(
        "--source-first",
        action="store_true",
        help="Source-first: skip text unless source/file lookup is empty; exclude tests",
    )
    lookup_parser.add_argument("--exclude-tests", action="store_true", help="Exclude test files from lookup hits")
    lookup_parser.add_argument("--json", action="store_true", help="Emit JSON")
    lookup_parser.set_defaults(func=_cmd_lookup)

    workspace_lookup_parser = subparsers.add_parser(
        "workspace-lookup",
        help="Search symbols and source text across multiple repository catalogs",
    )
    workspace_lookup_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to search; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_lookup_parser.add_argument("--workspace", help="Named workspace to search")
    workspace_lookup_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_lookup_parser.add_argument("query", help="Symbol or text query")
    workspace_lookup_parser.add_argument("--limit", type=int, default=20, help="Maximum combined hits")
    workspace_lookup_parser.add_argument("--per-repo-limit", type=int, help="Maximum hits requested per repository")
    workspace_lookup_parser.add_argument("--symbol-limit", type=int, help="Maximum symbol hits to consider per repo")
    workspace_lookup_parser.add_argument("--file-limit", type=int, help="Maximum file path hits to consider per repo")
    workspace_lookup_parser.add_argument(
        "--text-limit", type=int, help="Maximum text hits to consider per repo; use 0 to skip text"
    )
    workspace_lookup_parser.add_argument("--context", type=int, default=0, help="Context lines for text hits")
    workspace_lookup_parser.add_argument(
        "--source-first",
        action="store_true",
        help="Source-first: skip text unless source/file lookup is empty; exclude tests",
    )
    workspace_lookup_parser.add_argument(
        "--exclude-tests", action="store_true", help="Exclude test files from lookup hits"
    )
    workspace_lookup_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_lookup_parser.set_defaults(func=_cmd_workspace_lookup)

    workspace_references_parser = subparsers.add_parser(
        "workspace-references",
        help="Find exact source references across multiple repository catalogs",
    )
    workspace_references_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to search; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_references_parser.add_argument("--workspace", help="Named workspace to search")
    workspace_references_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_references_parser.add_argument("query", help="Identifier or text fragment to find")
    workspace_references_parser.add_argument("--limit", type=int, default=50, help="Maximum combined references")
    workspace_references_parser.add_argument("--per-repo-limit", type=int, help="Maximum references per repository")
    workspace_references_parser.add_argument(
        "--context", type=int, default=1, help="Context lines before and after each match"
    )
    workspace_references_parser.add_argument(
        "--no-definitions", action="store_true", help="Exclude definition lines from results"
    )
    workspace_references_parser.add_argument("--ignore-case", action="store_true", help="Ignore case when filtering")
    workspace_references_parser.add_argument(
        "--summary-only", action="store_true", help="Return one compact row per matched file"
    )
    workspace_references_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_references_parser.set_defaults(func=_cmd_workspace_references)

    context_pack_parser = subparsers.add_parser("context-pack", help="Return compact source context for a lookup query")
    _add_repo_and_db_args(context_pack_parser)
    context_pack_parser.add_argument("query", help="Symbol or text query")
    context_pack_parser.add_argument("--limit", type=int, default=10, help="Maximum ranked hits to consider")
    context_pack_parser.add_argument("--max-files", type=int, default=5, help="Maximum distinct files to include")
    context_pack_parser.add_argument("--context-lines", type=int, default=4, help="Lines before and after text hits")
    context_pack_parser.add_argument(
        "--max-lines-per-file", type=int, default=80, help="Maximum lines per selected file"
    )
    context_pack_parser.add_argument(
        "--source-first",
        action="store_true",
        help="Source-first: skip text unless source/file lookup is empty; exclude tests",
    )
    context_pack_parser.add_argument(
        "--exclude-tests", action="store_true", help="Do not spend context snippet slots on test files"
    )
    context_pack_parser.add_argument("--symbol-limit", type=int, help="Maximum symbol hits to consider")
    context_pack_parser.add_argument("--file-limit", type=int, help="Maximum file path hits to consider")
    context_pack_parser.add_argument("--text-limit", type=int, help="Maximum text hits to consider; use 0 to skip text")
    context_pack_parser.add_argument("--json", action="store_true", help="Emit JSON")
    context_pack_parser.set_defaults(func=_cmd_context_pack)

    workspace_context_parser = subparsers.add_parser(
        "workspace-context",
        help="Return compact source context for a workspace lookup query",
    )
    workspace_context_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to search; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_context_parser.add_argument("--workspace", help="Named workspace to search")
    workspace_context_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_context_parser.add_argument("query", help="Symbol or text query")
    workspace_context_parser.add_argument("--limit", type=int, default=10, help="Maximum combined hits to consider")
    workspace_context_parser.add_argument("--per-repo-limit", type=int, help="Maximum hits requested per repository")
    workspace_context_parser.add_argument("--max-files", type=int, default=5, help="Maximum distinct files to include")
    workspace_context_parser.add_argument(
        "--context-lines", type=int, default=4, help="Lines before and after text hits"
    )
    workspace_context_parser.add_argument(
        "--max-lines-per-file", type=int, default=80, help="Maximum lines per selected file"
    )
    workspace_context_parser.add_argument(
        "--source-first",
        action="store_true",
        help="Source-first: skip text unless source/file lookup is empty; exclude tests",
    )
    workspace_context_parser.add_argument(
        "--exclude-tests", action="store_true", help="Do not spend context snippet slots on test files"
    )
    workspace_context_parser.add_argument("--symbol-limit", type=int, help="Maximum symbol hits to consider per repo")
    workspace_context_parser.add_argument("--file-limit", type=int, help="Maximum file path hits to consider per repo")
    workspace_context_parser.add_argument(
        "--text-limit", type=int, help="Maximum text hits to consider per repo; use 0 to skip text"
    )
    workspace_context_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_context_parser.set_defaults(func=_cmd_workspace_context)

    explain_parser = subparsers.add_parser("explain", help="Explain a file change")
    _add_repo_and_db_args(explain_parser)
    explain_parser.add_argument("path", help="Cataloged file path")
    explain_parser.add_argument("--json", action="store_true", help="Emit JSON")
    explain_parser.set_defaults(func=_cmd_explain)

    tests_parser = subparsers.add_parser("tests", help="Show tests likely related to a file")
    _add_repo_and_db_args(tests_parser)
    tests_parser.add_argument("path", help="Cataloged file path")
    tests_parser.set_defaults(func=_cmd_tests)

    outline_parser = subparsers.add_parser("outline", help="Show symbols declared by one cataloged file")
    _add_repo_and_db_args(outline_parser)
    outline_parser.add_argument("path", help="Cataloged file path")
    outline_parser.add_argument("--json", action="store_true", help="Emit JSON")
    outline_parser.set_defaults(func=_cmd_outline)

    tree_parser = subparsers.add_parser("tree", help="Show a compact catalog-backed file tree")
    _add_repo_and_db_args(tree_parser)
    tree_parser.add_argument("--prefix", default="", help="Repository-relative prefix to inspect")
    tree_parser.add_argument("--max-depth", type=int, default=3, help="Maximum path depth")
    tree_parser.add_argument("--max-entries", type=int, default=200, help="Maximum entries")
    tree_parser.add_argument("--json", action="store_true", help="Emit JSON")
    tree_parser.set_defaults(func=_cmd_tree)

    repo_outline_parser = subparsers.add_parser("repo-outline", help="Show repository-level catalog summary")
    _add_repo_and_db_args(repo_outline_parser)
    repo_outline_parser.add_argument("--max-depth", type=int, default=2, help="Maximum directory summary depth")
    repo_outline_parser.add_argument("--top-files", type=int, default=20, help="Maximum symbol-heavy files")
    repo_outline_parser.add_argument("--json", action="store_true", help="Emit JSON")
    repo_outline_parser.set_defaults(func=_cmd_repo_outline)

    workspace_outline_parser = subparsers.add_parser("workspace-outline", help="Show multi-repository catalog summary")
    workspace_outline_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to include; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_outline_parser.add_argument("--workspace", help="Named workspace to inspect")
    workspace_outline_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_outline_parser.add_argument("--max-depth", type=int, default=2, help="Maximum directory summary depth")
    workspace_outline_parser.add_argument("--top-files", type=int, default=10, help="Maximum symbol-heavy files")
    workspace_outline_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_outline_parser.set_defaults(func=_cmd_workspace_outline)

    content_parser = subparsers.add_parser("content", help="Show bounded content from one cataloged file")
    _add_repo_and_db_args(content_parser)
    content_parser.add_argument("path", help="Cataloged file path")
    content_parser.add_argument("--start-line", type=int, help="One-based first line to include")
    content_parser.add_argument("--end-line", type=int, help="One-based final line to include")
    content_parser.add_argument("--json", action="store_true", help="Emit JSON")
    content_parser.set_defaults(func=_cmd_content)

    risk_parser = subparsers.add_parser("risk", help="Show highest-risk files")
    _add_repo_and_db_args(risk_parser)
    risk_parser.add_argument("--top", type=int, default=20, help="Number of files to show")
    risk_parser.set_defaults(func=_cmd_risk)

    savings_parser = subparsers.add_parser("savings", help="Show estimated code-intel lookup savings")
    _add_repo_and_db_args(savings_parser)
    savings_parser.add_argument("--json", action="store_true", help="Emit JSON")
    savings_parser.set_defaults(func=_cmd_savings)

    benchmark_parser = subparsers.add_parser("benchmark", help="Compare code-intel providers on real symbol lookups")
    _add_repo_and_db_args(benchmark_parser)
    benchmark_parser.add_argument("--query", action="append", default=[], help="Search query to benchmark")
    benchmark_parser.add_argument(
        "--queries-file",
        help="File with one benchmark query per line; blank lines and comments are ignored",
    )
    benchmark_parser.add_argument(
        "--provider",
        action="append",
        choices=("catalog", "jcodemunch"),
        default=[],
        help="Provider to benchmark; repeat to compare multiple providers",
    )
    benchmark_parser.add_argument("--limit", type=int, default=20, help="Maximum results requested per query")
    benchmark_parser.add_argument("--repeat", type=int, default=5, help="Measured repetitions per query/provider")
    benchmark_parser.add_argument("--warmup", type=int, default=1, help="Unmeasured warmup repetitions")
    benchmark_parser.add_argument(
        "--mode",
        choices=("symbol", "text", "lookup"),
        default="symbol",
        help="Benchmark symbol provider search, text search, or unified lookup",
    )
    benchmark_parser.add_argument("--json", action="store_true", help="Emit JSON")
    benchmark_parser.add_argument("--summary", action="store_true", help="Emit compact benchmark JSON with --json")
    benchmark_parser.set_defaults(func=_cmd_benchmark)

    workspace_benchmark_parser = subparsers.add_parser(
        "workspace-benchmark",
        help="Benchmark workspace lookup across multiple repository catalogs",
    )
    workspace_benchmark_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to search; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_benchmark_parser.add_argument("--workspace", help="Named workspace to benchmark")
    workspace_benchmark_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_benchmark_parser.add_argument("--query", action="append", default=[], help="Search query to benchmark")
    workspace_benchmark_parser.add_argument(
        "--queries-file",
        help="File with one benchmark query per line; blank lines and comments are ignored",
    )
    workspace_benchmark_parser.add_argument("--limit", type=int, default=20, help="Maximum combined hits per query")
    workspace_benchmark_parser.add_argument("--repeat", type=int, default=5, help="Measured repetitions per query")
    workspace_benchmark_parser.add_argument("--warmup", type=int, default=1, help="Unmeasured warmup repetitions")
    workspace_benchmark_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_benchmark_parser.add_argument(
        "--summary", action="store_true", help="Emit compact benchmark JSON with --json"
    )
    workspace_benchmark_parser.set_defaults(func=_cmd_workspace_benchmark)

    workflow_benchmark_parser = subparsers.add_parser(
        "workflow-benchmark",
        help="Benchmark lookup plus compact context retrieval for agent workflows",
    )
    workflow_benchmark_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to search; repeat for backend/UI or multi-repo workspaces",
    )
    workflow_benchmark_parser.add_argument("--workspace", help="Named workspace to benchmark")
    workflow_benchmark_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workflow_benchmark_parser.add_argument("--query", action="append", default=[], help="Search query to benchmark")
    workflow_benchmark_parser.add_argument(
        "--queries-file",
        help="File with one benchmark query per line; blank lines and comments are ignored",
    )
    workflow_benchmark_parser.add_argument("--limit", type=int, default=10, help="Maximum ranked hits per query")
    workflow_benchmark_parser.add_argument("--per-repo-limit", type=int, help="Maximum hits requested per repository")
    workflow_benchmark_parser.add_argument("--repeat", type=int, default=5, help="Measured repetitions per query")
    workflow_benchmark_parser.add_argument("--warmup", type=int, default=1, help="Unmeasured warmup repetitions")
    workflow_benchmark_parser.add_argument("--max-files", type=int, default=5, help="Maximum snippet files per query")
    workflow_benchmark_parser.add_argument("--context-lines", type=int, default=4, help="Lines before and after hits")
    workflow_benchmark_parser.add_argument(
        "--max-lines-per-file", type=int, default=80, help="Maximum snippet lines per selected file"
    )
    workflow_benchmark_parser.add_argument(
        "--source-first",
        action="store_true",
        help="Source-first: skip text unless source/file lookup is empty; exclude tests",
    )
    workflow_benchmark_parser.add_argument(
        "--exclude-tests", action="store_true", help="Do not spend context snippet slots on test files"
    )
    workflow_benchmark_parser.add_argument("--symbol-limit", type=int, help="Maximum symbol hits to consider per repo")
    workflow_benchmark_parser.add_argument("--file-limit", type=int, help="Maximum file path hits to consider per repo")
    workflow_benchmark_parser.add_argument(
        "--text-limit", type=int, help="Maximum text hits to consider per repo; use 0 to skip text"
    )
    workflow_benchmark_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workflow_benchmark_parser.add_argument(
        "--summary", action="store_true", help="Emit compact benchmark JSON with --json"
    )
    workflow_benchmark_parser.set_defaults(func=_cmd_workflow_benchmark)

    benchmark_suite_save_parser = subparsers.add_parser(
        "benchmark-suite-save",
        help="Save a repeatable real-world benchmark suite",
    )
    benchmark_suite_save_parser.add_argument("name", help="Benchmark suite name")
    benchmark_suite_save_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to include; repeat for backend/UI or multi-repo suites",
    )
    benchmark_suite_save_parser.add_argument("--workspace", help="Named workspace whose repositories should be saved")
    benchmark_suite_save_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    benchmark_suite_save_parser.add_argument(
        "--suite-dir",
        help="Directory for benchmark suite JSON files (default: ~/.code-intel/benchmark-suites)",
    )
    benchmark_suite_save_parser.add_argument(
        "--symbol-repo",
        help="Repository used for catalog-vs-jCodemunch symbol comparisons; defaults to the first repo",
    )
    benchmark_suite_save_parser.add_argument(
        "--symbol-query",
        action="append",
        default=[],
        help="Symbol provider comparison query; repeat for multiple queries",
    )
    benchmark_suite_save_parser.add_argument(
        "--workflow-query",
        action="append",
        default=[],
        help="Workflow lookup-to-context query; repeat for multiple queries",
    )
    benchmark_suite_save_parser.add_argument("--description", default="", help="Benchmark suite description")
    benchmark_suite_save_parser.add_argument("--limit", type=int, default=5, help="Default hit limit for suite runs")
    benchmark_suite_save_parser.add_argument(
        "--repeat", type=int, default=5, help="Default measured repetitions per query"
    )
    benchmark_suite_save_parser.add_argument(
        "--warmup", type=int, default=1, help="Default unmeasured warmup repetitions"
    )
    benchmark_suite_save_parser.add_argument("--per-repo-limit", type=int, help="Workflow hits requested per repo")
    benchmark_suite_save_parser.add_argument(
        "--max-files", type=int, default=3, help="Default maximum workflow snippet files"
    )
    benchmark_suite_save_parser.add_argument(
        "--context-lines", type=int, default=3, help="Default workflow context lines"
    )
    benchmark_suite_save_parser.add_argument(
        "--max-lines-per-file", type=int, default=40, help="Default maximum workflow lines per file"
    )
    benchmark_suite_save_parser.add_argument(
        "--no-source-first",
        action="store_false",
        dest="source_first",
        help="Allow workflow suites to include tests and normal text fanout",
    )
    benchmark_suite_save_parser.set_defaults(func=_cmd_benchmark_suite_save, source_first=True)
    benchmark_suite_save_parser.add_argument("--json", action="store_true", help="Emit JSON")

    benchmark_suite_list_parser = subparsers.add_parser(
        "benchmark-suite-list",
        help="List saved benchmark suites",
    )
    benchmark_suite_list_parser.add_argument(
        "--suite-dir",
        help="Directory for benchmark suite JSON files (default: ~/.code-intel/benchmark-suites)",
    )
    benchmark_suite_list_parser.add_argument("--json", action="store_true", help="Emit JSON")
    benchmark_suite_list_parser.set_defaults(func=_cmd_benchmark_suite_list)

    benchmark_suite_run_parser = subparsers.add_parser(
        "benchmark-suite-run",
        help="Run a saved benchmark suite",
    )
    benchmark_suite_run_parser.add_argument("name", help="Benchmark suite name")
    benchmark_suite_run_parser.add_argument(
        "--suite-dir",
        help="Directory for benchmark suite JSON files (default: ~/.code-intel/benchmark-suites)",
    )
    benchmark_suite_run_parser.add_argument(
        "--provider",
        action="append",
        choices=("catalog", "jcodemunch"),
        default=[],
        help="Symbol provider to benchmark; repeat to compare multiple providers",
    )
    benchmark_suite_run_parser.add_argument("--limit", type=int, help="Override suite hit limit")
    benchmark_suite_run_parser.add_argument("--repeat", type=int, help="Override measured repetitions")
    benchmark_suite_run_parser.add_argument("--warmup", type=int, help="Override unmeasured warmup repetitions")
    benchmark_suite_run_parser.add_argument("--json", action="store_true", help="Emit JSON")
    benchmark_suite_run_parser.add_argument(
        "--summary", action="store_true", help="Emit compact suite benchmark JSON with --json"
    )
    benchmark_suite_run_parser.add_argument(
        "--record-history",
        action="store_true",
        help="Append this run scorecard to benchmark suite history",
    )
    benchmark_suite_run_parser.add_argument(
        "--history-dir",
        help="Directory for benchmark suite run history JSONL files (default: ~/.code-intel/benchmark-runs)",
    )
    benchmark_suite_run_parser.set_defaults(func=_cmd_benchmark_suite_run)

    benchmark_suite_history_parser = subparsers.add_parser(
        "benchmark-suite-history",
        help="Show saved benchmark suite run history",
    )
    benchmark_suite_history_parser.add_argument("name", help="Benchmark suite name")
    benchmark_suite_history_parser.add_argument(
        "--history-dir",
        help="Directory for benchmark suite run history JSONL files (default: ~/.code-intel/benchmark-runs)",
    )
    benchmark_suite_history_parser.add_argument("--limit", type=int, default=10, help="Most-recent records to show")
    benchmark_suite_history_parser.add_argument("--json", action="store_true", help="Emit JSON")
    benchmark_suite_history_parser.set_defaults(func=_cmd_benchmark_suite_history)

    workspace_scan_parser = subparsers.add_parser(
        "workspace-scan",
        help="Catalog several repositories in one workspace refresh",
    )
    workspace_scan_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository path to catalog; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_scan_parser.add_argument("--workspace", help="Named workspace to catalog")
    workspace_scan_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_scan_parser.add_argument(
        "--incremental",
        action="store_true",
        help="Reuse unchanged file analyses when possible",
    )
    workspace_scan_parser.add_argument(
        "--skip-unchanged-meta",
        action="store_true",
        help="Skip metadata-only writes when incremental scans find no file changes",
    )
    workspace_scan_parser.add_argument("--workers", type=int, help="Analysis worker threads per repository")
    workspace_scan_parser.add_argument(
        "--repo-workers",
        type=int,
        help="Repositories to scan concurrently; defaults to a small bounded worker count",
    )
    workspace_scan_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_scan_parser.set_defaults(func=_cmd_workspace_scan)

    workspace_save_parser = subparsers.add_parser("workspace-save", help="Save a named multi-repo workspace")
    workspace_save_parser.add_argument("name", help="Workspace name")
    workspace_save_parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repository path to include; repeat for backend/UI or multi-repo workspaces",
    )
    workspace_save_parser.add_argument(
        "--workspace-dir",
        help="Directory for named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_save_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_save_parser.set_defaults(func=_cmd_workspace_save)

    workspace_list_parser = subparsers.add_parser("workspace-list", help="List named multi-repo workspaces")
    workspace_list_parser.add_argument(
        "--workspace-dir",
        help="Directory for named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    workspace_list_parser.add_argument("--json", action="store_true", help="Emit JSON")
    workspace_list_parser.set_defaults(func=_cmd_workspace_list)

    doctor_parser = subparsers.add_parser("doctor", help="Inspect repository/catalog health")
    doctor_parser.add_argument("repo", nargs="?", default=".", help="Repository path")
    doctor_parser.add_argument("--db", help=f"Database path (default: <repo>/{DEFAULT_CATALOG_PATH})")
    doctor_parser.add_argument("--summary", action="store_true", help="Emit compact health JSON")
    doctor_parser.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON")
    doctor_parser.set_defaults(func=_cmd_doctor)

    refresh_job_parser = subparsers.add_parser(
        "install-refresh-job",
        help="Install a macOS launchd job that refreshes catalogs with incremental scans",
    )
    refresh_job_parser.add_argument("repos", nargs="*", help="Repository paths to refresh")
    refresh_job_parser.add_argument("--workspace", help="Named workspace whose repositories should be refreshed")
    refresh_job_parser.add_argument(
        "--workspace-dir",
        help="Directory containing named workspace JSON files (default: ~/.code-intel/workspaces)",
    )
    refresh_job_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_REFRESH_INTERVAL_MINUTES,
        help=f"Refresh cadence in minutes (default: {DEFAULT_REFRESH_INTERVAL_MINUTES})",
    )
    refresh_job_parser.add_argument("--label", default=DEFAULT_REFRESH_LABEL, help="launchd job label")
    refresh_job_parser.add_argument(
        "--command",
        default="code-intel",
        help="Command used to invoke code-intel when --project is not supplied",
    )
    refresh_job_parser.add_argument(
        "--project",
        help="code-intel source checkout for `uv run --project <path> code-intel`",
    )
    refresh_job_parser.add_argument(
        "--install-dir",
        help="Directory for generated runner scripts and logs (default: ~/.code-intel/launchd)",
    )
    refresh_job_parser.add_argument(
        "--launch-agents-dir",
        help="Directory for LaunchAgent plists (default: ~/Library/LaunchAgents)",
    )
    refresh_job_parser.add_argument(
        "--dry-run", action="store_true", help="Print the planned job without writing files"
    )
    refresh_job_parser.add_argument("--json", action="store_true", help="Emit JSON")
    refresh_job_parser.set_defaults(func=_cmd_install_refresh_job)

    notes_parser = subparsers.add_parser("install-agent-notes", help="Upsert code-intel guidance into agent files")
    notes_parser.add_argument("repo", nargs="?", default=".", help="Repository path")
    notes_parser.add_argument(
        "--command-prefix",
        default="code-intel",
        help="Command agents should use to invoke code-intel",
    )
    notes_parser.add_argument("--dry-run", action="store_true", help="Show planned writes without changing files")
    notes_parser.set_defaults(func=_cmd_install_agent_notes)

    mcp_parser = subparsers.add_parser("serve-mcp", help="Run the code-intel MCP server over stdio")
    mcp_parser.add_argument("--repo", default=".", help="Default repository path for MCP tools")
    mcp_parser.set_defaults(func=_cmd_serve_mcp)

    return parser


def _add_repo_and_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Repository path")
    parser.add_argument("--db", help=f"Database path (default: <repo>/{DEFAULT_CATALOG_PATH})")


def _cmd_scan(args: argparse.Namespace) -> int:
    result = build_catalog(
        args.repo,
        args.db,
        incremental=args.incremental,
        workers=args.workers,
        skip_unchanged_meta=args.skip_unchanged_meta,
    )
    if args.json:
        print(json.dumps(asdict(result), indent=2))
        return 0

    scan_detail = ""
    if result.incremental:
        scan_detail = (
            f" (incremental: reused={result.reused_file_count}, "
            f"changed={result.changed_file_count}, removed={result.removed_file_count})"
        )
    print(
        f"Cataloged {result.file_count} files, {result.symbol_count} symbols, "
        f"{result.dependency_count} dependencies in {result.timings_ms.get('total', 0.0):.1f} ms "
        f"-> {result.database_path}{scan_detail}"
    )
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    provider: ProviderName = args.provider
    if provider == "catalog":
        _require_catalog(store)
    result = search_symbols(repo_root, store, args.query, limit=args.limit, provider=provider)
    if not result.symbols:
        _record_usage_event(
            store,
            tool="find",
            provider=result.provider,
            query=args.query,
            result_count=0,
            selected_paths=set(),
        )
        print("No symbols found.")
        return 0

    _record_usage_event(
        store,
        tool="find",
        provider=result.provider,
        query=args.query,
        result_count=len(result.symbols),
        selected_paths=selected_paths_from_rows(result.symbols),
    )
    for row in result.symbols:
        print(f"{row['path']}:{row['line']} {row['kind']} {row['qualified_name']}")
        if row["signature"]:
            print(f"  {row['signature']}")
    return 0


def _cmd_search_text(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    _require_catalog(store)
    matches = store.search_text(args.query, limit=args.limit, context_lines=args.context)
    _record_usage_event(
        store,
        tool="search-text",
        provider="catalog",
        query=args.query,
        result_count=len(matches),
        selected_paths=selected_paths_from_text_matches(matches),
    )
    if args.json:
        print(json.dumps([asdict(match) for match in matches], indent=2))
        return 0
    if not matches:
        print("No text matches found.")
        return 0
    for match in matches:
        print(f"{match.path}:{match.line} {match.content.strip()}")
        if args.context and match.snippet:
            print(match.snippet)
    return 0


def _cmd_references(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    result = find_references(
        repo_root,
        store,
        args.query,
        limit=args.limit,
        context_lines=args.context,
        include_definitions=not args.no_definitions,
        ignore_case=args.ignore_case,
    )
    _record_usage_event(
        store,
        tool="references",
        provider="catalog",
        query=args.query,
        result_count=len(result.matches),
        selected_paths=reference_selected_paths(result),
        returned_tokens=result.summary_tokens if args.summary_only else result.estimated_tokens,
    )
    if args.json:
        print(json.dumps(reference_result_to_dict(result, summary_only=args.summary_only), indent=2))
        return 0
    if args.summary_only:
        _print_reference_summaries(result.file_summaries)
        return 0
    _print_references(result.matches)
    return 0


def _cmd_lookup(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    result = lookup(
        repo_root,
        store,
        args.query,
        limit=args.limit,
        symbol_limit=args.symbol_limit,
        file_limit=args.file_limit,
        text_limit=_context_text_limit(args),
        fallback_text_limit=_context_fallback_text_limit(args),
        context_lines=args.context,
        include_tests=_context_include_tests(args),
    )
    _record_usage_event(
        store,
        tool="lookup",
        provider="catalog",
        query=args.query,
        result_count=len(result.hits),
        selected_paths=lookup_selected_paths(result),
    )
    if args.json:
        print(json.dumps(lookup_to_dict(result), indent=2))
        return 0
    if not result.hits:
        print("No lookup results found.")
        return 0
    for hit in result.hits:
        print(f"{hit.path}:{hit.line} {hit.kind} {hit.label}")
        if hit.detail:
            print(f"  {hit.detail}")
    return 0


def _cmd_workspace_lookup(args: argparse.Namespace) -> int:
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    result = workspace_lookup(
        repo_paths,
        args.query,
        limit=args.limit,
        per_repo_limit=args.per_repo_limit,
        symbol_limit=args.symbol_limit,
        file_limit=args.file_limit,
        text_limit=_context_text_limit(args),
        fallback_text_limit=_context_fallback_text_limit(args),
        context_lines=args.context,
        include_tests=_context_include_tests(args),
    )
    hits_by_repo = _workspace_hit_counts_by_repo(result)
    for repo_path, selected_paths in workspace_selected_paths_by_repo(result).items():
        store = CatalogStore.for_repo(Path(repo_path))
        _record_usage_event(
            store,
            tool="workspace-lookup",
            provider="catalog",
            query=args.query,
            result_count=hits_by_repo.get(repo_path, 0),
            selected_paths=selected_paths,
        )

    if args.json:
        print(json.dumps(workspace_lookup_to_dict(result), indent=2))
        return 0

    for repo in result.repos:
        if not repo.searched:
            print(f"[{repo.label}] skipped: {repo.error}", file=sys.stderr)
    if not result.hits:
        print("No workspace lookup results found.")
        return 0
    for hit in result.hits:
        print(f"[{hit.repo_label}] {hit.path}:{hit.line} {hit.kind} {hit.label}")
        if hit.detail:
            print(f"  {hit.detail}")
    return 0


def _cmd_workspace_references(args: argparse.Namespace) -> int:
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    result = workspace_references(
        repo_paths,
        args.query,
        limit=args.limit,
        per_repo_limit=args.per_repo_limit,
        context_lines=args.context,
        include_definitions=not args.no_definitions,
        ignore_case=args.ignore_case,
    )
    selected_paths_by_repo = workspace_reference_selected_paths(result)
    token_counts_by_repo = workspace_reference_tokens_by_repo(result)
    summary_token_counts_by_repo = workspace_reference_summary_tokens_by_repo(result)
    counts_by_repo = workspace_reference_counts_by_repo(result)
    for repo in result.repos:
        if not repo.searched:
            continue
        store = CatalogStore.for_repo(Path(repo.repo_path))
        _record_usage_event(
            store,
            tool="workspace-references",
            provider="catalog",
            query=args.query,
            result_count=counts_by_repo.get(repo.repo_path, 0),
            selected_paths=selected_paths_by_repo.get(repo.repo_path, set()),
            returned_tokens=(
                summary_token_counts_by_repo.get(repo.repo_path, 0)
                if args.summary_only
                else token_counts_by_repo.get(repo.repo_path, 0)
            ),
        )

    if args.json:
        print(json.dumps(workspace_reference_result_to_dict(result, summary_only=args.summary_only), indent=2))
        return 0

    for repo in result.repos:
        if not repo.searched:
            print(f"[{repo.label}] skipped: {repo.error}", file=sys.stderr)
    if args.summary_only:
        _print_reference_summaries(result.file_summaries, show_repo=True)
        return 0
    _print_references(result.matches, show_repo=True)
    return 0


def _cmd_context_pack(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    pack = build_context_pack(
        repo_root,
        store,
        args.query,
        limit=args.limit,
        max_files=args.max_files,
        context_lines=args.context_lines,
        max_lines_per_file=args.max_lines_per_file,
        symbol_limit=args.symbol_limit,
        file_limit=args.file_limit,
        text_limit=_context_text_limit(args),
        fallback_text_limit=_context_fallback_text_limit(args),
        include_tests=_context_include_tests(args),
    )
    _record_usage_event(
        store,
        tool="context-pack",
        provider="catalog",
        query=args.query,
        result_count=pack.hit_count,
        selected_paths={snippet.path for snippet in pack.snippets},
        returned_tokens=pack.estimated_tokens,
    )
    if args.json:
        print(json.dumps(context_pack_to_dict(pack), indent=2))
        return 0

    _print_context_pack(pack)
    return 0


def _cmd_workspace_context(args: argparse.Namespace) -> int:
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    pack = build_workspace_context_pack(
        repo_paths,
        args.query,
        limit=args.limit,
        per_repo_limit=args.per_repo_limit,
        max_files=args.max_files,
        context_lines=args.context_lines,
        max_lines_per_file=args.max_lines_per_file,
        symbol_limit=args.symbol_limit,
        file_limit=args.file_limit,
        text_limit=_context_text_limit(args),
        fallback_text_limit=_context_fallback_text_limit(args),
        include_tests=_context_include_tests(args),
    )
    selected_paths_by_repo = context_pack_selected_paths(pack)
    token_counts_by_repo = context_pack_snippet_tokens_by_repo(pack)
    hit_counts_by_repo = context_pack_hit_counts_by_repo(pack)
    for repo_path in pack.repo_paths:
        store = CatalogStore.for_repo(Path(repo_path))
        _record_usage_event(
            store,
            tool="workspace-context",
            provider="catalog",
            query=args.query,
            result_count=hit_counts_by_repo.get(repo_path, 0),
            selected_paths=selected_paths_by_repo.get(repo_path, set()),
            returned_tokens=token_counts_by_repo.get(repo_path, 0),
        )
    if args.json:
        print(json.dumps(context_pack_to_dict(pack), indent=2))
        return 0

    _print_context_pack(pack)
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    report = compute_change_report(repo_root, store, args.path)
    _record_usage_event(
        store,
        tool="explain",
        provider="catalog",
        target_path=report.path,
        result_count=len(report.direct_dependents) + len(report.transitive_dependents) + len(report.related_tests),
        selected_paths=selected_paths_from_change_report(report),
    )
    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0

    print(f"File: {report.path}")
    print(f"Risk: {report.risk} (score {report.blast_score})")
    _print_group("Direct dependents", report.direct_dependents)
    _print_group("Transitive dependents", report.transitive_dependents)
    if report.related_tests:
        print("\nRelated tests")
        for match in report.related_tests:
            print(f"  {match.path} ({match.reason})")
    return 0


def _cmd_tests(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    target_path = store.resolve_file_path(args.path)
    if target_path is None:
        raise ValueError(f"File is not cataloged: {args.path}")
    matches = find_related_tests(repo_root, store, target_path)
    _record_usage_event(
        store,
        tool="tests",
        provider="catalog",
        target_path=target_path,
        result_count=len(matches),
        selected_paths=selected_paths_from_test_matches(target_path, matches),
    )
    if not matches:
        print("No related tests found.")
        return 0
    for match in matches:
        print(f"{match.path} - {match.reason}")
    return 0


def _cmd_outline(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    outline = get_file_outline(repo_root, store, args.path)
    if args.json:
        print(json.dumps(outline, indent=2))
        return 0

    print(f"File: {outline['path']}")
    print(f"Language: {outline['language']}  Lines: {outline['line_count']}  Symbols: {outline['symbol_count']}")
    for symbol in outline["symbols"]:
        line = symbol["line"]
        end_line = symbol["end_line"]
        line_range = f"{line}-{end_line}" if end_line and end_line != line else str(line)
        print(f"  {line_range:<9} {symbol['kind']:<8} {symbol['qualified_name']}")
        if symbol["signature"]:
            print(f"            {symbol['signature']}")
    return 0


def _cmd_tree(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    tree = get_file_tree(
        repo_root,
        store,
        prefix=args.prefix,
        max_depth=args.max_depth,
        max_entries=args.max_entries,
    )
    _record_usage_event(
        store,
        tool="tree",
        provider="catalog",
        target_path=args.prefix,
        result_count=int(tree["returned_entries"]),
        selected_paths=_selected_paths_from_tree(tree),
        returned_tokens=_estimated_payload_tokens(tree),
    )
    if args.json:
        print(json.dumps(tree, indent=2))
        return 0
    _print_tree(tree)
    return 0


def _cmd_repo_outline(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    outline = get_repo_outline(repo_root, store, max_depth=args.max_depth, top_files=args.top_files)
    _record_usage_event(
        store,
        tool="repo-outline",
        provider="catalog",
        result_count=len(outline["directories"]) + len(outline["top_files"]),
        selected_paths={str(row["path"]) for row in outline["top_files"]},
        returned_tokens=_estimated_payload_tokens(outline),
    )
    if args.json:
        print(json.dumps(outline, indent=2))
        return 0
    _print_repo_outline(outline)
    return 0


def _cmd_workspace_outline(args: argparse.Namespace) -> int:
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    outline = get_workspace_outline(repo_paths, max_depth=args.max_depth, top_files=args.top_files)
    _record_workspace_outline_usage(outline)
    if args.json:
        print(json.dumps(outline, indent=2))
        return 0
    _print_workspace_outline(outline)
    return 0


def _cmd_content(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    result = get_file_content(
        repo_root,
        store,
        args.path,
        start_line=args.start_line,
        end_line=args.end_line,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(result["content"])
    return 0


def _cmd_risk(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    rows = top_risk_files(repo_root, store, args.top)
    _record_usage_event(
        store,
        tool="risk",
        provider="catalog",
        result_count=len(rows),
        selected_paths={row.path for row in rows},
    )
    if not rows:
        print("No cataloged source files found.")
        return 0
    for row in rows:
        print(
            f"{row.score:>5} {row.risk:<8} direct={row.direct_dependents:<3} "
            f"transitive={row.transitive_dependents:<3} tests={row.related_tests:<3} {row.path}"
        )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = CatalogStore.for_repo(repo_root, Path(args.db).resolve() if args.db else None)
    print(json.dumps(assess_catalog_health(repo_root, store, summary_only=args.summary), indent=2))
    return 0


def _cmd_workspace_scan(args: argparse.Namespace) -> int:
    repos = _workspace_repos_from_args(args, repo_attr="repo")
    report = scan_workspace(
        repos,
        incremental=args.incremental,
        workers=args.workers,
        repo_workers=args.repo_workers,
        skip_unchanged_meta=args.skip_unchanged_meta,
    )
    if args.json:
        print(json.dumps(workspace_scan_to_dict(report), indent=2))
        return 1 if report.failed_count else 0

    print(
        f"Workspace scan: {report.scanned_count}/{report.repo_count} repos in "
        f"{report.total_ms:.1f} ms (repo_workers={report.repo_workers})"
    )
    for result in report.repos:
        label = Path(result.repo_path).name or result.repo_path
        if not result.ok:
            print(f"  {label}: ERROR {result.error}")
            continue
        catalog = result.catalog
        timings = catalog.get("timings_ms", {})
        total_ms = timings.get("total", 0.0) if isinstance(timings, dict) else 0.0
        print(
            f"  {label}: files={catalog.get('file_count', 0)} symbols={catalog.get('symbol_count', 0)} "
            f"reused={catalog.get('reused_file_count', 0)} changed={catalog.get('changed_file_count', 0)} "
            f"written={catalog.get('written_file_count', 0)} total={float(total_ms):.1f} ms"
        )
    return 1 if report.failed_count else 0


def _cmd_install_refresh_job(args: argparse.Namespace) -> int:
    repos = _workspace_repos_from_args(args, repo_attr="repos")
    job = build_launchd_refresh_job(
        repos,
        label=args.label,
        interval_minutes=args.interval_minutes,
        command=args.command,
        project_path=args.project,
        install_dir=args.install_dir,
        launch_agents_dir=args.launch_agents_dir,
    )
    if not args.dry_run:
        write_launchd_refresh_job(job)

    if args.json:
        payload = job.to_dict()
        payload["written"] = not args.dry_run
        print(json.dumps(payload, indent=2))
        return 0

    action = "Planned" if args.dry_run else "Installed"
    print(f"{action} code-intel refresh job: {job.label}")
    print(f"Interval: every {job.interval_minutes} minutes")
    print(f"Script: {job.script_path}")
    print(f"Plist: {job.plist_path}")
    print("Repos:")
    for repo in job.repos:
        print(f"  {repo}")
    if args.dry_run:
        print("\nDry run: no files written.")
    else:
        print("\nLoad with:")
        print(f"  launchctl load {job.plist_path}")
        print("Run once with:")
        print(f"  launchctl start {job.label}")
    return 0


def _cmd_savings(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    summary = store.usage_summary()
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    avoided_files = max(0, int(summary["candidate_files"]) - int(summary["returned_files"]))
    print(f"Usage events: {summary['events']}")
    print(f"Estimated saved tokens: {summary['estimated_saved_tokens']:,}")
    print(f"Estimated avoided file reads: {avoided_files:,}")
    if not summary["events"]:
        print("No lookup events recorded yet.")
        return 0

    if summary["by_tool"]:
        print("\nBy tool")
        for row in summary["by_tool"]:
            print(
                f"  {row['tool']} ({row['provider']}): "
                f"{row['events']} events, {int(row['estimated_saved_tokens']):,} tokens"
            )

    if summary["recent"]:
        print("\nRecent")
        for row in summary["recent"]:
            subject = row["query"] or row["target_path"] or "-"
            print(
                f"  {row['created_at']} {row['tool']} ({row['provider']}): "
                f"{subject} -> {row['result_count']} results, "
                f"{int(row['estimated_saved_tokens']):,} tokens"
            )
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    queries = [*args.query, *_queries_from_file(args.queries_file)]
    providers = tuple(args.provider or DEFAULT_BENCHMARK_PROVIDERS)
    report = run_benchmark(
        args.repo,
        queries=queries,
        providers=providers,
        mode=args.mode,
        limit=args.limit,
        repeat=args.repeat,
        warmup=args.warmup,
    )
    if args.json:
        print(report_to_summary_json(report) if args.summary else report_to_json(report))
        return 0

    print(f"Repo: {report.repo_path}")
    print(f"Mode: {report.mode}")
    print(f"Providers: {', '.join(report.providers)}")
    print(f"Limit: {report.limit}  Repeat: {report.repeat}  Warmup: {report.warmup}")
    print("\nProvider health")
    for health in report.health:
        details = health.details
        symbols = details.get("symbols", "-")
        files = details.get("files", "-")
        indexed_at = details.get("indexed_at") or details.get("meta", {}).get("generated_at", "")
        print(
            f"  {health.provider}: available={health.available} files={files} symbols={symbols} indexed_at={indexed_at}"
        )

    print("\nQueries")
    for comparison in report.queries:
        print(f"  {comparison.query}")
        for run in comparison.runs:
            paths = ", ".join(run.selected_paths[:3])
            if len(run.selected_paths) > 3:
                paths += f", +{len(run.selected_paths) - 3} more"
            print(
                f"    {run.provider:<10} median={run.median_ms:>8.3f}ms avg={run.avg_ms:>8.3f}ms "
                f"results={run.result_count:<3} files={run.returned_files:<3} saved={run.estimated_saved_tokens:,} "
                f"src={run.source_path_count:<2} tests={run.test_path_count:<2} "
                f"first={run.first_result_path or '-'} paths={paths or '-'}"
            )
        for provider, overlap in comparison.overlap.items():
            if provider == "baseline" or not isinstance(overlap, dict):
                continue
            print(
                f"    overlap vs {comparison.overlap['baseline']} -> {provider}: "
                f"shared={overlap['shared_path_count']} jaccard={overlap['jaccard']}"
            )
    return 0


def _cmd_workspace_benchmark(args: argparse.Namespace) -> int:
    queries = [*args.query, *_queries_from_file(args.queries_file)]
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    report = run_workspace_benchmark(
        repo_paths,
        queries=queries,
        limit=args.limit,
        repeat=args.repeat,
        warmup=args.warmup,
    )
    if args.json:
        print(workspace_report_to_summary_json(report) if args.summary else workspace_report_to_json(report))
        return 0

    print(f"Repos: {', '.join(report.repo_paths)}")
    print(f"Mode: {report.mode}")
    print(f"Limit: {report.limit}  Repeat: {report.repeat}  Warmup: {report.warmup}")
    print("\nCatalog health")
    for health in report.health:
        details = health.details
        symbols = details.get("symbols", "-")
        files = details.get("files", "-")
        indexed_at = details.get("meta", {}).get("generated_at", "")
        print(
            f"  {health.provider}: available={health.available} files={files} symbols={symbols} indexed_at={indexed_at}"
        )

    print("\nQueries")
    for comparison in report.queries:
        print(f"  {comparison.query}")
        for run in comparison.runs:
            paths = ", ".join(run.selected_paths[:3])
            if len(run.selected_paths) > 3:
                paths += f", +{len(run.selected_paths) - 3} more"
            print(
                f"    {run.provider:<10} median={run.median_ms:>8.3f}ms avg={run.avg_ms:>8.3f}ms "
                f"results={run.result_count:<3} files={run.returned_files:<3} saved={run.estimated_saved_tokens:,} "
                f"src={run.source_path_count:<2} tests={run.test_path_count:<2} "
                f"first={run.first_result_path or '-'} paths={paths or '-'}"
            )
    return 0


def _cmd_workflow_benchmark(args: argparse.Namespace) -> int:
    queries = [*args.query, *_queries_from_file(args.queries_file)]
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    report = run_workflow_benchmark(
        repo_paths,
        queries=queries,
        limit=args.limit,
        repeat=args.repeat,
        warmup=args.warmup,
        per_repo_limit=args.per_repo_limit,
        max_files=args.max_files,
        context_lines=args.context_lines,
        max_lines_per_file=args.max_lines_per_file,
        include_tests=_context_include_tests(args),
        symbol_limit=args.symbol_limit,
        file_limit=args.file_limit,
        text_limit=_context_text_limit(args),
        fallback_text_limit=_context_fallback_text_limit(args),
    )
    if args.json:
        print(workflow_report_to_summary_json(report) if args.summary else workflow_report_to_json(report))
        return 0

    print(f"Repos: {', '.join(report.repo_paths)}")
    print(f"Mode: {report.mode}")
    print(
        f"Limit: {report.limit}  Repeat: {report.repeat}  Warmup: {report.warmup}  "
        f"Max files: {report.max_files}  Max lines/file: {report.max_lines_per_file}"
    )
    print("\nCatalog health")
    for health in report.health:
        details = health.details
        symbols = details.get("symbols", "-")
        files = details.get("files", "-")
        indexed_at = details.get("meta", {}).get("generated_at", "")
        print(
            f"  {health.provider}: available={health.available} files={files} symbols={symbols} indexed_at={indexed_at}"
        )

    print("\nQueries")
    for run in report.queries:
        paths = ", ".join(run.selected_paths[:3])
        if len(run.selected_paths) > 3:
            paths += f", +{len(run.selected_paths) - 3} more"
        print(
            f"  {run.query}: median={run.median_ms:>8.3f}ms avg={run.avg_ms:>8.3f}ms "
            f"hits={run.hit_count:<3} files={run.selected_files:<3} lines={run.selected_lines:<4} "
            f"tokens={run.estimated_tokens:<5} saved={run.estimated_saved_tokens:,} "
            f"bytes={run.payload_bytes:,} src={run.source_path_count:<2} tests={run.test_path_count:<2} "
            f"first={run.first_result_path or '-'} paths={paths or '-'}"
        )
    return 0


def _cmd_benchmark_suite_save(args: argparse.Namespace) -> int:
    repo_paths = _workspace_repos_from_args(args, repo_attr="repo")
    config = save_benchmark_suite_config(
        args.name,
        repo_paths,
        symbol_queries=args.symbol_query,
        workflow_queries=args.workflow_query,
        symbol_repo=args.symbol_repo,
        workspace=args.workspace or "",
        description=args.description,
        config_dir=args.suite_dir,
        limit=args.limit,
        repeat=args.repeat,
        warmup=args.warmup,
        per_repo_limit=args.per_repo_limit,
        max_files=args.max_files,
        context_lines=args.context_lines,
        max_lines_per_file=args.max_lines_per_file,
        source_first=args.source_first,
    )
    if args.json:
        print(json.dumps(benchmark_suite_config_to_dict(config), indent=2, sort_keys=True))
        return 0

    print(f"Saved benchmark suite {config.name}: {config.path}")
    print(f"Repos: {', '.join(config.repos)}")
    if config.symbol_queries:
        print(f"Symbol repo: {config.symbol_repo}")
        print(f"Symbol queries: {', '.join(config.symbol_queries)}")
    if config.workflow_queries:
        print(f"Workflow queries: {', '.join(config.workflow_queries)}")
    return 0


def _cmd_benchmark_suite_list(args: argparse.Namespace) -> int:
    configs = list_benchmark_suite_configs(config_dir=args.suite_dir)
    payload = {"count": len(configs), "suites": [benchmark_suite_config_to_dict(config) for config in configs]}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not configs:
        print("No benchmark suites configured.")
        return 0
    for config in configs:
        print(
            f"{config.name}: repos={len(config.repos)} symbol_queries={len(config.symbol_queries)} "
            f"workflow_queries={len(config.workflow_queries)} path={config.path}"
        )
    return 0


def _cmd_benchmark_suite_run(args: argparse.Namespace) -> int:
    config = load_benchmark_suite_config(args.name, config_dir=args.suite_dir)
    providers = tuple(args.provider or DEFAULT_BENCHMARK_PROVIDERS)
    run = run_benchmark_suite(
        config,
        providers=providers,
        limit=args.limit,
        repeat=args.repeat,
        warmup=args.warmup,
    )
    history_record = record_benchmark_suite_run(run, history_dir=args.history_dir) if args.record_history else None
    if args.json:
        payload = benchmark_suite_run_to_dict(run, summary=args.summary)
        if history_record is not None:
            payload["history"] = _benchmark_suite_history_record_to_dict(history_record)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    _print_benchmark_suite_run(run)
    if history_record is not None:
        print(f"\nRecorded history: {history_record.path}")
        _print_benchmark_suite_delta(history_record.delta)
    return 0


def _cmd_benchmark_suite_history(args: argparse.Namespace) -> int:
    records = load_benchmark_suite_history(args.name, history_dir=args.history_dir, limit=args.limit)
    payload = {"count": len(records), "history": records}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not records:
        print(f"No benchmark suite history found for {args.name}.")
        return 0
    for record in records:
        scorecard = record.get("scorecard", {})
        workflow = scorecard.get("workflow", {}) if isinstance(scorecard, dict) else {}
        symbol = scorecard.get("symbol", {}) if isinstance(scorecard, dict) else {}
        comparisons = symbol.get("comparisons", []) if isinstance(symbol, dict) else []
        speedup = "-"
        if isinstance(comparisons, list) and comparisons and isinstance(comparisons[0], dict):
            speedup = f"{float(comparisons[0].get('baseline_speedup', 0.0)):>.3f}x"
        print(
            f"{record.get('recorded_at', '')} providers={', '.join(record.get('providers', []))} "
            f"speedup={speedup} workflow_median={float(workflow.get('median_ms_avg', 0.0)):>.3f}ms "
            f"workflow_source={workflow.get('source_first_count', 0)}/{workflow.get('query_count', 0)} "
            f"saved={int(workflow.get('estimated_saved_tokens_total', 0)):,}"
        )
    return 0


def _cmd_workspace_save(args: argparse.Namespace) -> int:
    config = save_workspace_config(args.name, args.repo, config_dir=args.workspace_dir)
    if args.json:
        print(json.dumps(workspace_config_to_dict(config), indent=2))
        return 0

    print(f"Saved workspace {config.name}: {config.path}")
    for repo in config.repos:
        print(f"  {repo}")
    return 0


def _cmd_workspace_list(args: argparse.Namespace) -> int:
    configs = list_workspace_configs(config_dir=args.workspace_dir)
    if args.json:
        print(json.dumps([workspace_config_to_dict(config) for config in configs], indent=2))
        return 0

    if not configs:
        print("No workspaces configured.")
        return 0
    for config in configs:
        print(f"{config.name}: {', '.join(config.repos)}")
    return 0


def _cmd_install_agent_notes(args: argparse.Namespace) -> int:
    results = install_agent_notes(
        args.repo,
        command_prefix=args.command_prefix,
        dry_run=args.dry_run,
    )
    for result in results:
        print(f"{result.action}: {result.path}")
        if result.action == "skipped-same-target":
            print(f"  target already handled: {result.target}")
    return 0


def _cmd_serve_mcp(args: argparse.Namespace) -> int:
    serve_mcp(args.repo)
    return 0


def _store_from_args(args: argparse.Namespace) -> CatalogStore:
    repo_root = Path(args.repo).resolve()
    return CatalogStore.for_repo(repo_root, Path(args.db).resolve() if args.db else None)


def _require_catalog(store: CatalogStore) -> None:
    if not store.has_catalog():
        raise FileNotFoundError(f"No catalog found at {store.database_path}. Run `code-intel scan` first.")


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
    if not store.has_catalog():
        return
    if returned_tokens is None:
        metrics = estimate_saved_tokens_for_paths(store, selected_paths, result_count)
    else:
        metrics = estimate_saved_tokens_for_context_pack(store, selected_paths, returned_tokens, result_count)
    store.record_usage_event(
        tool=tool,
        provider=provider,
        query=query,
        target_path=target_path,
        result_count=result_count,
        **metrics,
    )


def _workspace_hit_counts_by_repo(result: WorkspaceLookupResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in result.hits:
        counts[hit.repo_path] = counts.get(hit.repo_path, 0) + 1
    return counts


def _record_workspace_outline_usage(outline: dict[str, object]) -> None:
    repos = outline.get("repos", [])
    if not isinstance(repos, list):
        return
    for repo in repos:
        if not isinstance(repo, dict) or not repo.get("searched"):
            continue
        repo_path = str(repo["repo_path"])
        store = CatalogStore.for_repo(Path(repo_path))
        top_files = repo.get("top_files", [])
        selected_paths = {str(row["path"]) for row in top_files if isinstance(row, dict)}
        _record_usage_event(
            store,
            tool="workspace-outline",
            provider="catalog",
            result_count=len(selected_paths),
            selected_paths=selected_paths,
            returned_tokens=_estimated_payload_tokens(repo),
        )


def _workspace_repos_from_args(args: argparse.Namespace, *, repo_attr: str) -> list[str]:
    repos = getattr(args, repo_attr, None) or []
    return resolve_workspace_repos(
        repos,
        workspace_name=getattr(args, "workspace", None),
        config_dir=getattr(args, "workspace_dir", None),
    )


def _context_text_limit(args: argparse.Namespace) -> int | None:
    if getattr(args, "source_first", False):
        return 0
    return getattr(args, "text_limit", None)


def _context_fallback_text_limit(args: argparse.Namespace) -> int | None:
    if not getattr(args, "source_first", False):
        return None
    explicit_text_limit = getattr(args, "text_limit", None)
    if explicit_text_limit is not None:
        return explicit_text_limit
    return getattr(args, "limit", None)


def _context_include_tests(args: argparse.Namespace) -> bool:
    return not (getattr(args, "source_first", False) or getattr(args, "exclude_tests", False))


def _print_context_pack(pack: ContextPack) -> None:
    print(
        f"Query: {pack.query}  hits={pack.hit_count} files={pack.selected_files} "
        f"lines={pack.selected_lines} est_tokens={pack.estimated_tokens} "
        f"saved_tokens={pack.estimated_saved_tokens}"
    )
    if not pack.snippets:
        print("No context snippets found.")
        return
    for snippet in pack.snippets:
        print(f"\n[{snippet.repo_label}] {snippet.path}:{snippet.start_line}-{snippet.end_line}")
        print(_numbered_content(snippet.content, start_line=snippet.start_line))


def _print_benchmark_suite_run(run: BenchmarkSuiteRun) -> None:
    config = run.config
    print(f"Suite: {config.name}")
    if config.description:
        print(f"Description: {config.description}")
    print(f"Repos: {', '.join(config.repos)}")
    print(f"Providers: {', '.join(run.providers)}")
    _print_benchmark_suite_scorecard(benchmark_suite_scorecard(run))
    if run.symbol_report is not None:
        print("\nSymbol provider comparison")
        for comparison in run.symbol_report.queries:
            print(f"  {comparison.query}")
            for query_run in comparison.runs:
                print(
                    f"    {query_run.provider:<10} median={query_run.median_ms:>8.3f}ms "
                    f"results={query_run.result_count:<3} src={query_run.source_path_count:<2} "
                    f"tests={query_run.test_path_count:<2} first={query_run.first_result_path or '-'}"
                )
    if run.workflow_report is not None:
        print("\nWorkflow context")
        for workflow_run in run.workflow_report.queries:
            paths = ", ".join(workflow_run.selected_paths[:3])
            if len(workflow_run.selected_paths) > 3:
                paths += f", +{len(workflow_run.selected_paths) - 3} more"
            print(
                f"  {workflow_run.query}: median={workflow_run.median_ms:>8.3f}ms "
                f"files={workflow_run.selected_files:<3} tokens={workflow_run.estimated_tokens:<5} "
                f"saved={workflow_run.estimated_saved_tokens:,} first={workflow_run.first_result_path or '-'} "
                f"paths={paths or '-'}"
            )


def _print_benchmark_suite_scorecard(scorecard: dict[str, object]) -> None:
    print("\nScorecard")
    symbol = scorecard.get("symbol", {})
    if isinstance(symbol, dict):
        providers = symbol.get("providers", {})
        if isinstance(providers, dict):
            for provider, stats in providers.items():
                if not isinstance(stats, dict):
                    continue
                print(
                    f"  symbol {provider}: median_avg={float(stats.get('median_ms_avg', 0.0)):>7.3f}ms "
                    f"source_first={stats.get('source_first_count', 0)}/{stats.get('query_count', 0)} "
                    f"test_first={stats.get('test_first_count', 0)}"
                )
        comparisons = symbol.get("comparisons", [])
        if isinstance(comparisons, list):
            for comparison in comparisons:
                if not isinstance(comparison, dict):
                    continue
                print(
                    f"  comparison {comparison.get('baseline')} vs {comparison.get('provider')}: "
                    f"speedup={float(comparison.get('baseline_speedup', 0.0)):>.3f}x "
                    f"source_delta={comparison.get('baseline_source_first_delta', 0)} "
                    f"provider_test_delta={comparison.get('provider_test_first_delta', 0)}"
                )
    workflow = scorecard.get("workflow", {})
    if isinstance(workflow, dict):
        print(
            f"  workflow: median_avg={float(workflow.get('median_ms_avg', 0.0)):>7.3f}ms "
            f"source_first={workflow.get('source_first_count', 0)}/{workflow.get('query_count', 0)} "
            f"test_first={workflow.get('test_first_count', 0)} "
            f"tokens={workflow.get('estimated_tokens_total', 0)} "
            f"saved={int(workflow.get('estimated_saved_tokens_total', 0)):,}"
        )


def _benchmark_suite_history_record_to_dict(record: BenchmarkSuiteHistoryRecord) -> dict[str, object]:
    return {
        "path": record.path,
        "entry": record.entry,
        "previous_entry": record.previous_entry,
        "delta": record.delta,
    }


def _print_benchmark_suite_delta(delta: dict[str, object] | None) -> None:
    if delta is None:
        print("History delta: no previous run")
        return

    workflow = delta.get("workflow", {})
    if isinstance(workflow, dict):
        print(
            f"History delta: workflow_median={float(workflow.get('median_ms_avg_delta', 0.0)):>+.3f}ms "
            f"source_first={int(workflow.get('source_first_delta', 0)):+d} "
            f"test_first={int(workflow.get('test_first_delta', 0)):+d} "
            f"tokens={int(workflow.get('estimated_tokens_total_delta', 0)):+d}"
        )
    symbol = delta.get("symbol", {})
    comparisons = symbol.get("comparisons", []) if isinstance(symbol, dict) else []
    if isinstance(comparisons, list) and comparisons and isinstance(comparisons[0], dict):
        comparison = comparisons[0]
        print(
            f"History delta: speedup={float(comparison.get('baseline_speedup_delta', 0.0)):>+.3f}x "
            f"source_delta={int(comparison.get('baseline_source_first_delta_delta', 0)):+d} "
            f"provider_test_delta={int(comparison.get('provider_test_first_delta_delta', 0)):+d}"
        )


def _print_references(matches: list[ReferenceMatch], *, show_repo: bool = False) -> None:
    if not matches:
        print("No references found.")
        return
    for match in matches:
        prefix = f"[{match.repo_label}] " if show_repo else ""
        symbol_detail = f" {match.symbol_kind}" if match.symbol_kind else ""
        print(f"{prefix}{match.path}:{match.line} {match.kind}{symbol_detail} {match.content.strip()}")
        if match.snippet and match.snippet != match.content:
            print(_numbered_content(match.snippet, start_line=match.start_line))


def _print_reference_summaries(summaries: list[ReferenceFileSummary], *, show_repo: bool = False) -> None:
    if not summaries:
        print("No references found.")
        return
    for summary in summaries:
        prefix = f"[{summary.repo_label}] " if show_repo else ""
        print(
            f"{prefix}{summary.path}:{summary.first_line}-{summary.last_line} "
            f"matches={summary.match_count} definitions={summary.definition_count} "
            f"references={summary.reference_count} {summary.sample_content.strip()}"
        )


def _print_tree(tree: dict[str, object]) -> None:
    prefix = str(tree["prefix"]) or "."
    print(
        f"Tree: {prefix} files={tree['total_files']} entries={tree['returned_entries']} truncated={tree['truncated']}"
    )
    for entry in tree["entries"]:
        if not isinstance(entry, dict):
            continue
        if entry["type"] == "directory":
            print(
                f"  {'  ' * int(entry['depth'])}{entry['path']}/ "
                f"files={entry['file_count']} symbols={entry['symbol_count']} lines={entry['line_count']}"
            )
        else:
            print(
                f"  {'  ' * int(entry['depth'])}{entry['path']} "
                f"{entry['language']} symbols={entry['symbol_count']} lines={entry['line_count']}"
            )


def _print_repo_outline(outline: dict[str, object]) -> None:
    print(
        f"Repo: {outline['repo_path']} files={outline['total_files']} "
        f"symbols={outline['total_symbols']} lines={outline['total_lines']}"
    )
    print("\nLanguages")
    for row in outline["languages"]:
        if isinstance(row, dict):
            print(f"  {row['language']}: files={row['file_count']} lines={row['line_count']}")
    print("\nDirectories")
    for row in outline["directories"]:
        if isinstance(row, dict):
            print(f"  {row['path']}/ files={row['file_count']} symbols={row['symbol_count']} lines={row['line_count']}")
    print("\nTop files")
    for row in outline["top_files"]:
        if isinstance(row, dict):
            print(f"  {row['path']} symbols={row['symbol_count']} lines={row['line_count']}")


def _print_workspace_outline(outline: dict[str, object]) -> None:
    print(
        f"Workspace: repos={outline['searched_repos']}/{outline['repo_count']} "
        f"files={outline['total_files']} symbols={outline['total_symbols']} lines={outline['total_lines']}"
    )
    print("\nRepos")
    for repo in outline["repos"]:
        if not isinstance(repo, dict):
            continue
        if not repo["searched"]:
            print(f"  {repo['label']}: skipped {repo['error']}")
            continue
        print(
            f"  {repo['label']}: files={repo['total_files']} "
            f"symbols={repo['total_symbols']} lines={repo['total_lines']}"
        )
    print("\nLanguages")
    for row in outline["languages"]:
        if isinstance(row, dict):
            print(f"  {row['language']}: files={row['file_count']} lines={row['line_count']}")
    print("\nTop files")
    for row in outline["top_files"]:
        if isinstance(row, dict):
            print(f"  [{row['repo_label']}] {row['path']} symbols={row['symbol_count']} lines={row['line_count']}")


def _numbered_content(content: str, *, start_line: int) -> str:
    return "\n".join(f"{line_number:>5}: {line}" for line_number, line in enumerate(content.splitlines(), start_line))


def _print_group(label: str, values: list[str]) -> None:
    if not values:
        return
    print(f"\n{label} ({len(values)})")
    for value in values:
        print(f"  {value}")


def _queries_from_file(path: str | None) -> list[str]:
    if not path:
        return []
    query_path = Path(path)
    queries: list[str] = []
    for line in query_path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            queries.append(stripped)
    return queries


def _selected_paths_from_tree(tree: dict[str, object]) -> set[str]:
    entries = tree.get("entries", [])
    if not isinstance(entries, list):
        return set()
    return {str(entry["path"]) for entry in entries if isinstance(entry, dict) and entry.get("type") == "file"}


def _estimated_payload_tokens(payload: object) -> int:
    return max(0, (len(json.dumps(payload, sort_keys=True)) + 3) // 4)
