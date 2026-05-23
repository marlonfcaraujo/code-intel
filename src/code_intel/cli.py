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
    store = _store_from_args(args)
    _require_catalog(store)
    rows = store.search_symbols(args.query, args.limit)
    if not rows:
        print("No symbols found.")
        return 0

    for row in rows:
        print(f"{row['path']}:{row['line']} {row['kind']} {row['qualified_name']}")
        if row["signature"]:
            print(f"  {row['signature']}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    store = _store_from_args(args)
    _require_catalog(store)
    report = compute_change_report(repo_root, store, args.path)
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
    }
    if store.database_path.exists():
        checks["meta"] = store.get_meta()
        checks["files"] = store.file_count()
        checks["symbols"] = store.symbol_count()
        checks["dependencies"] = store.dependency_count()
    print(json.dumps(checks, indent=2))
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
    if not store.database_path.exists():
        raise FileNotFoundError(f"No catalog found at {store.database_path}. Run `code-intel scan` first.")


def _print_group(label: str, values: list[str]) -> None:
    if not values:
        return
    print(f"\n{label} ({len(values)})")
    for value in values:
        print(f"  {value}")
