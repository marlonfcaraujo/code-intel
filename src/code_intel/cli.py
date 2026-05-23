"""Command line interface for code-intel."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from code_intel.agent_notes import install_agent_notes
from code_intel.catalog_store import DEFAULT_CATALOG_PATH, CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.change_report import compute_change_report
from code_intel.mcp_server import serve_mcp
from code_intel.risk import top_risk_files
from code_intel.savings import (
    estimate_saved_tokens_for_paths,
    selected_paths_from_change_report,
    selected_paths_from_rows,
    selected_paths_from_test_matches,
)
from code_intel.symbol_search import ProviderName, search_symbols
from code_intel.tests_map import find_related_tests


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
    scan_parser.set_defaults(func=_cmd_scan)

    find_parser = subparsers.add_parser("find", help="Search cataloged symbols")
    _add_repo_and_db_args(find_parser)
    find_parser.add_argument("query", help="Symbol query")
    find_parser.add_argument("--limit", type=int, default=20, help="Maximum results")
    find_parser.add_argument(
        "--provider",
        choices=("auto", "catalog", "jcodemunch"),
        default="auto",
        help="Symbol provider to use (default: auto)",
    )
    find_parser.set_defaults(func=_cmd_find)

    explain_parser = subparsers.add_parser("explain", help="Explain a file change")
    _add_repo_and_db_args(explain_parser)
    explain_parser.add_argument("path", help="Cataloged file path")
    explain_parser.add_argument("--json", action="store_true", help="Emit JSON")
    explain_parser.set_defaults(func=_cmd_explain)

    tests_parser = subparsers.add_parser("tests", help="Show tests likely related to a file")
    _add_repo_and_db_args(tests_parser)
    tests_parser.add_argument("path", help="Cataloged file path")
    tests_parser.set_defaults(func=_cmd_tests)

    risk_parser = subparsers.add_parser("risk", help="Show highest-risk files")
    _add_repo_and_db_args(risk_parser)
    risk_parser.add_argument("--top", type=int, default=20, help="Number of files to show")
    risk_parser.set_defaults(func=_cmd_risk)

    savings_parser = subparsers.add_parser("savings", help="Show estimated code-intel lookup savings")
    _add_repo_and_db_args(savings_parser)
    savings_parser.add_argument("--json", action="store_true", help="Emit JSON")
    savings_parser.set_defaults(func=_cmd_savings)

    doctor_parser = subparsers.add_parser("doctor", help="Inspect repository/catalog health")
    doctor_parser.add_argument("repo", nargs="?", default=".", help="Repository path")
    doctor_parser.add_argument("--db", help=f"Database path (default: <repo>/{DEFAULT_CATALOG_PATH})")
    doctor_parser.set_defaults(func=_cmd_doctor)

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
    result = build_catalog(args.repo, args.db)
    print(
        f"Cataloged {result.file_count} files, {result.symbol_count} symbols, "
        f"{result.dependency_count} dependencies -> {result.database_path}"
    )
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    provider: ProviderName = args.provider
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
    checks: dict[str, Any] = {
        "repo": str(repo_root),
        "repo_exists": repo_root.exists(),
        "git_repo": (repo_root / ".git").exists(),
        "database": str(store.database_path),
        "database_exists": store.database_path.exists(),
        "catalog_exists": store.has_catalog(),
    }
    if store.has_catalog():
        checks["meta"] = store.get_meta()
        checks["files"] = store.file_count()
        checks["symbols"] = store.symbol_count()
        checks["dependencies"] = store.dependency_count()
    if store.database_path.exists():
        checks["usage"] = store.usage_summary()
    print(json.dumps(checks, indent=2))
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


def _print_group(label: str, values: list[str]) -> None:
    if not values:
        return
    print(f"\n{label} ({len(values)})")
    for value in values:
        print(f"  {value}")
