"""Reproducible, read-only Codex pilot against a pinned public source checkout.

Run with the code-intel environment and its optional MCP dependency installed.
Raw transcripts remain under the chosen local output directory.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_intel.agent_execution import run_private
from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.context_pack import build_context_pack
from code_intel.integrations import private_json
from code_intel.lookup import lookup as catalog_lookup
from code_intel.measured_usage import measured_report
from code_intel.usage_adapters import parse_agent_usage

TASKS = {
    "integration_defaults": {
        "question": "Find the function that supplies missing integration configuration defaults. Return JSON keys "
        "path, function, enabled_default (boolean), timeout_seconds_default (integer), schema_version (integer).",
        "expected": {
            "path": "src/code_intel/config_upgrade.py",
            "function": "migrate_config",
            "enabled_default": True,
            "timeout_seconds_default": 300,
            "schema_version": 2,
        },
    },
    "refresh_schedule": {
        "question": "Find the default scheduled index refresh interval and how the launchd job represents it. "
        "Return JSON keys path, function, interval_minutes (integer), interval_seconds (integer), "
        "interval_key, run_at_load (boolean).",
        "expected": {
            "path": "src/code_intel/refresh_job.py",
            "function": "build_launchd_refresh_job",
            "interval_minutes": 180,
            "interval_seconds": 10800,
            "interval_key": "StartInterval",
            "run_at_load": True,
        },
    },
    "catalog_publication": {
        "question": "Locate the method that atomically publishes a full replacement catalog. Identify the helper "
        "that preserves usage history, the write-lock helper, and whether copying history occurs before file "
        "replacement. Return JSON keys path, method (Class.method), history_helper, lock_helper, "
        "history_before_replace (boolean).",
        "expected": {
            "path": "src/code_intel/catalog_store.py",
            "method": "CatalogStore.publish_catalog",
            "history_helper": "_copy_usage_events",
            "lock_helper": "_catalog_write_lock",
            "history_before_replace": True,
        },
    },
}
BASE_TOOLS = {"list_files", "search_text", "read_file"}
EXTRA_TOOLS = {"lookup", "context_pack"}
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "plugins",
    "apps",
    "memories",
    "chronicle",
    "multi_agent",
    "multi_agent_v2",
    "hooks",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "skill_search",
    "skill_mcp_dependency_install",
    "code_mode",
    "view_image",
    "tool_suggest",
)


def source_path(root: Path, path: str) -> Path:
    """Restrict every model-requested file read to Python source under src."""
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root / "src") or candidate.suffix != ".py" or not candidate.is_file():
        raise ValueError("Only repository Python source files are accessible")
    return candidate


def make_server(root: Path, arm: str, audit_path: Path):
    """Expose equal source tools and optional bounded code-intel capabilities."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    root = root.resolve()
    server = FastMCP("pilot", log_level="ERROR")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    files = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*.py")
        if path.resolve().is_relative_to(root / "src")
    )

    def record(tool: str, started: float, result: object) -> object:
        with open(audit_path, "a", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(
                json.dumps(
                    {
                        "tool": tool,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                        "result_bytes": len(json.dumps(result).encode()),
                    }
                )
                + "\n"
            )
        return result

    @server.tool(annotations=read_only)
    def list_files(pattern: str = "*") -> list[str]:
        """List repository Python source paths matching a glob."""
        started = time.monotonic()
        return record("list_files", started, [path for path in files if fnmatch.fnmatch(path, pattern)])

    @server.tool(annotations=read_only)
    def search_text(pattern: str, path_glob: str = "*", limit: int = 20) -> list[dict[str, Any]]:
        """Search source with a case-insensitive regex and return matching lines, like grep."""
        started = time.monotonic()
        if len(pattern) > 200:
            raise ValueError("Pattern too long")
        regex = re.compile(pattern, re.IGNORECASE)
        hits = []
        for path in files:
            if fnmatch.fnmatch(path, path_glob):
                for number, line in enumerate(source_path(root, path).read_text().splitlines(), 1):
                    if regex.search(line):
                        hits.append({"path": path, "line": number, "content": line[:600]})
                        if len(hits) >= max(1, min(limit, 50)):
                            return record("search_text", started, hits)
        return record("search_text", started, hits)

    @server.tool(annotations=read_only)
    def read_file(path: str, start_line: int = 1, line_count: int = 100) -> dict[str, Any]:
        """Read numbered source lines; at most 200 lines per call."""
        started = time.monotonic()
        lines = source_path(root, path).read_text().splitlines()
        start = max(1, start_line)
        selected = lines[start - 1 : start - 1 + max(1, min(line_count, 200))]
        return record(
            "read_file",
            started,
            {
                "path": path,
                "total_lines": len(lines),
                "content": "\n".join(f"{start + i}: {line}" for i, line in enumerate(selected))[:20000],
            },
        )

    if arm == "code_intel":
        store = CatalogStore.for_repo(root)

        @server.tool(annotations=read_only)
        def lookup(query: str) -> list[dict[str, Any]]:
            """Find ranked source symbols and files by name or text using code-intel."""
            started = time.monotonic()
            result = catalog_lookup(root, store, query, limit=10, include_tests=False)
            return record(
                "lookup",
                started,
                [
                    {"path": hit.path, "line": hit.line, "label": hit.label, "detail": hit.detail}
                    for hit in result.hits
                    if hit.path.startswith("src/")
                ],
            )

        @server.tool(annotations=read_only)
        def context_pack(query: str) -> list[dict[str, Any]]:
            """Get ranked bounded source snippets for a symbol or question using code-intel."""
            started = time.monotonic()
            pack = build_context_pack(root, store, query, max_files=3, max_lines_per_file=80, include_tests=False)
            return record(
                "context_pack",
                started,
                [
                    {
                        "path": snippet.path,
                        "start_line": snippet.start_line,
                        "end_line": snippet.end_line,
                        "content": snippet.content,
                    }
                    for snippet in pack.snippets
                    if snippet.path.startswith("src/")
                ],
            )

    return server


def grade(task: str, answer: str) -> bool:
    """Grade exact structured answers without asking another model."""
    answer = answer.strip()
    if answer.startswith("```json") and answer.endswith("```"):
        answer = answer[7:-3].strip()
    try:
        actual = json.loads(answer)
    except ValueError:
        return False
    expected = TASKS[task]["expected"]
    return isinstance(actual, dict) and all(
        type(actual.get(key)) is type(value) and actual[key] == value for key, value in expected.items()
    )


def codex_arguments(root: Path, arm: str, audit: Path, model: str) -> list[str]:
    """Build identical Codex settings except for the server's treatment arm."""
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--model",
        model,
    ]
    for feature in DISABLED_FEATURES:
        command.extend(["--disable", feature])
    overrides = {
        "approval_policy": "never",
        "model_reasoning_effort": "low",
        "project_doc_max_bytes": 0,
        "web_search": "disabled",
        "skills.max_context_tokens": 1,
        "tool_output_token_limit": 6000,
        "mcp_servers.pilot.command": sys.executable,
        "mcp_servers.pilot.args": [
            str(Path(__file__).resolve()),
            "server",
            "--root",
            str(root),
            "--arm",
            arm,
            "--audit",
            str(audit),
        ],
        "mcp_servers.pilot.required": True,
        "mcp_servers.pilot.startup_timeout_sec": 20,
    }
    for key, value in overrides.items():
        command.extend(["-c", f"{key}={json.dumps(value)}"])
    for tool in sorted(BASE_TOOLS | (EXTRA_TOOLS if arm == "code_intel" else set())):
        command.extend(["-c", f'mcp_servers.pilot.tools.{tool}.approval_mode="auto"'])
    return [*command, "-"]


def run(args: argparse.Namespace) -> None:
    """Run the pilot and retain transcripts privately for verification."""
    repo = args.repo.resolve()
    revision = run_private(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    receipts = []
    task_names = [args.task] if args.task else list(TASKS)
    for repetition in range(args.repeat):
        for index, task in enumerate(task_names):
            arms = ["baseline", "code_intel"] if (repetition + index) % 2 == 0 else ["code_intel", "baseline"]
            for arm in arms:
                run_id = f"{task}-{repetition}-{arm}"
                destination = output / run_id
                destination.mkdir(mode=0o700)
                with tempfile.TemporaryDirectory(prefix="code-intel-pilot-") as temporary:
                    root = Path(temporary) / "repo"
                    for command in (
                        ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--", str(repo), str(root)],
                        ["git", "-C", str(root), "checkout", "--quiet", "--detach", revision],
                    ):
                        result = run_private(command, cwd=Path(temporary))
                        if result.returncode:
                            raise RuntimeError("Could not prepare source checkout")
                    started = time.monotonic()
                    if arm == "code_intel":
                        build_catalog(root)
                    index_ms = round((time.monotonic() - started) * 1000, 3) if arm == "code_intel" else 0
                    audit = destination / "tools.jsonl"
                    prompt = (
                        "Inspect the repository source using the available tools. Prefer specialized context tools "
                        "when available. Do not guess. Return only the requested JSON object.\n"
                        + TASKS[task]["question"]
                    )
                    started = time.monotonic()
                    result = run_private(
                        codex_arguments(root, arm, audit, args.model), cwd=root, stdin=prompt, timeout=args.timeout
                    )
                    elapsed = round((time.monotonic() - started) * 1000)
                    for name, content in (("events.jsonl", result.stdout), ("stderr.txt", result.stderr)):
                        with open(
                            destination / name, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
                        ) as stream:
                            stream.write(content)
                    usage = parse_agent_usage("codex", result.stdout)
                    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                    tool_events = [
                        event["item"]
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("type") == "mcp_tool_call"
                    ]
                    forbidden = [
                        event
                        for event in events
                        if event.get("item", {}).get("type") in {"command_execution", "web_search", "file_change"}
                    ]
                    allowed = BASE_TOOLS | (EXTRA_TOOLS if arm == "code_intel" else set())
                    if (
                        forbidden
                        or not tool_events
                        or any(
                            event.get("server") != "pilot" or event.get("tool") not in allowed for event in tool_events
                        )
                    ):
                        raise RuntimeError("Tool trace failed isolation check; inspect private events")
                    audits = [json.loads(line) for line in audit.read_text().splitlines()]
                    success = result.returncode == 0 and usage.completed and grade(task, usage.answer)
                    record = {
                        "pair_id": f"{task}:{repetition}",
                        "arm": arm,
                        "model": args.model,
                        "revision": revision,
                        "prompt_id": task,
                        "config_id": "source-only-pilot-v1",
                        "elapsed_ms": elapsed,
                        "success": success,
                        "native_exit_code": result.returncode,
                        "calls": usage.calls,
                    }
                    records.append(record)
                    receipts.append(
                        {
                            "task": task,
                            "repeat": repetition,
                            "arm": arm,
                            "success": success,
                            "elapsed_ms": elapsed,
                            "index_ms": index_ms,
                            "tool_calls": len(tool_events),
                            "tool_names": [event["tool"] for event in tool_events],
                            "tool_result_bytes": sum(item["result_bytes"] for item in audits),
                            "usage": usage.calls[0]["usage"],
                        }
                    )
                    private_json(destination / "record.json", record)
                    print(json.dumps(receipts[-1]), flush=True)
    report = measured_report(records)
    report.update(
        {
            "source_revision": revision,
            "model": args.model,
            "reasoning_effort": "low",
            "recorded_at": datetime.now(UTC).isoformat(),
            "repetitions": args.repeat,
            "tool_trace_verified": True,
            "receipts": receipts,
            "cache_condition": "uncontrolled; fresh sessions do not guarantee cold cache",
        }
    )
    private_json(output / "report.json", report)
    print(json.dumps({"completed_pairs": report["paired_tasks"]}), flush=True)


def main() -> None:
    """Run either the restricted MCP source server or the paired experiment."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("server")
    server.add_argument("--root", type=Path, required=True)
    server.add_argument("--arm", choices=["baseline", "code_intel"], required=True)
    server.add_argument("--audit", type=Path, required=True)
    pilot = commands.add_parser("run")
    pilot.add_argument("--repo", type=Path, default=Path.cwd())
    pilot.add_argument("--output", type=Path, required=True)
    pilot.add_argument("--model", default="gpt-5.6-luna")
    pilot.add_argument("--repeat", type=int, default=2)
    pilot.add_argument("--timeout", type=int, default=180)
    pilot.add_argument("--task", choices=list(TASKS))
    args = parser.parse_args()
    if args.command == "server":
        make_server(args.root, args.arm, args.audit).run(transport="stdio")
    else:
        if args.repeat < 1:
            parser.error("repeat must be positive")
        run(args)


if __name__ == "__main__":
    main()
