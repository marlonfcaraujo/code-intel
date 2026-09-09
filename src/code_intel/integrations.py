"""Local integration registration and native usage imports."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from code_intel.agent_execution import execute_stdin
from code_intel.config_upgrade import read_config, update_config
from code_intel.usage_adapters import parse_agent_usage

AGENTS = ("codex", "hermes", "paseo", "opencode", "claude")
HELP_ARGS = {
    "codex": ["exec", "--help"],
    "hermes": ["--help"],
    "paseo": ["run", "--help"],
    "opencode": ["run", "--help"],
    "claude": ["--help"],
}
REQUIRED_FLAGS = {
    "codex": ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--sandbox"),
    "hermes": ("--usage-file", "--ignore-user-config", "--ignore-rules", "--safe-mode"),
    "paseo": ("--json",),
    "opencode": ("--format", "--model"),
    "claude": (
        "--output-format",
        "--no-session-persistence",
        "--setting-sources",
        "--strict-mcp-config",
        "--permission-mode",
    ),
}


def private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write private JSON with owner-only permissions.

    Args:
        path: Local destination outside public export workflows.
        payload: JSON-compatible configuration or private measurement record.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".measurement-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def probe_integration(name: str, executable: str | None = None) -> dict[str, Any]:
    """Check CLI flags without starting an agent or reading authentication.

    Args:
        name: Supported integration name.
        executable: Optional explicit local executable.

    Returns:
        Fixed-label capability data safe to display without private paths.
    """
    if name not in AGENTS:
        raise ValueError("Unsupported integration")
    binary = executable or shutil.which(name)
    available = False
    if binary:
        try:
            result = subprocess.run([binary, *HELP_ARGS[name]], capture_output=True, text=True, timeout=15)
            available = result.returncode == 0 and all(flag in result.stdout for flag in REQUIRED_FLAGS[name])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "integration": name,
        "installed": binary is not None,
        "cli_compatible": available,
        "task_usage_supported": name != "paseo",
        "authentication": "not_checked",
        "limitation": "latest_usage_only" if name == "paseo" else "native_reported_usage",
    }


def register_integrations(subparsers: argparse._SubParsersAction) -> None:
    """Register one-command setup, diagnostics and private imports."""
    parser = subparsers.add_parser("integrations", help="Configure native agent usage adapters")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".code-intel" / "integrations")
    actions = parser.add_subparsers(dest="integration_action", required=True)
    add = actions.add_parser("add", help="Discover a CLI and save a private adapter registration")
    add.add_argument("agent", choices=AGENTS)
    add.add_argument("--executable")
    actions.add_parser("list", help="List supported integrations and local registration state")
    doctor = actions.add_parser("doctor", help="Probe CLI compatibility without model calls")
    doctor.add_argument("agent", choices=AGENTS, nargs="?")
    importer = actions.add_parser("import", help="Convert native usage to a private task record")
    importer.add_argument("agent", choices=AGENTS)
    importer.add_argument("--input", type=Path, required=True)
    importer.add_argument("--metadata", type=Path, required=True)
    importer.add_argument("--output", type=Path, required=True)
    execute = actions.add_parser("execute", help="Run a prepared, graded task from JSON stdin (may incur model usage)")
    execute.add_argument("agent", choices=AGENTS)
    parser.set_defaults(func=_command)


def import_record(name: str, native: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Combine native usage with independently verified private task metadata.

    Args:
        name: Registered agent name.
        native: Native usage output.
        metadata: Pair, controls, elapsed time and external grader success.

    Returns:
        Private task record suitable for measured-report after JSONL serialization.
    """
    usage = parse_agent_usage(name, native)
    keys = ("pair_id", "arm", "model", "revision", "prompt_id", "config_id", "elapsed_ms", "success")
    record = {key: metadata[key] for key in keys}
    if type(record["success"]) is not bool or type(record["elapsed_ms"]) is not int or record["elapsed_ms"] < 0:
        raise ValueError("Invalid grading metadata")
    if record["arm"] not in ("baseline", "code_intel"):
        raise ValueError("Invalid arm")
    if any(not isinstance(record[key], str) or not record[key] for key in keys[:6]):
        raise ValueError("Missing task identity")
    record["success"] = record["success"] and usage.completed
    record["calls"] = usage.calls
    return record


def _command(args: argparse.Namespace) -> int:
    try:
        if args.integration_action == "execute":
            config = read_config(args.config_dir / f"{args.agent}.json", "integrations")
            if not config["enabled"]:
                raise ValueError("Integration is disabled")
            return execute_stdin(args.agent, config["executable"], timeout_seconds=config["timeout_seconds"])
        if args.integration_action == "import":
            record = import_record(args.agent, args.input.read_text(), json.loads(args.metadata.read_text()))
            # JSONL-compatible single-line record; metadata stays private on disk.
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if args.output.exists():
                raise ValueError("Destination exists")
            with args.output.open("x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                stream.write(json.dumps(record, allow_nan=False) + "\n")
            print(json.dumps({"imported": True, "integration": args.agent}))
            return 0
        if args.integration_action == "add":
            path = args.config_dir / f"{args.agent}.json"
            existing = read_config(path, "integrations") if path.exists() else {}
            executable = shutil.which(args.executable or existing.get("executable") or args.agent)
            status = probe_integration(args.agent, executable)
            if not status["cli_compatible"]:
                print(json.dumps(status))
                return 1
            update_config(path, "integrations", {"integration": args.agent, "executable": executable})
            print(json.dumps({**status, "registered": True}))
            return 0
        names = [args.agent] if getattr(args, "agent", None) else AGENTS
        statuses = []
        for name in names:
            path = args.config_dir / f"{name}.json"
            config = read_config(path, "integrations") if path.exists() else {}
            status = (
                probe_integration(name, config.get("executable"))
                if args.integration_action == "doctor"
                else {"integration": name, "task_usage_supported": name != "paseo"}
            )
            statuses.append({**status, "registered": path.exists()})
        print(json.dumps(statuses, indent=2))
        return int(args.integration_action == "doctor" and any(not item["cli_compatible"] for item in statuses))
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.TimeoutExpired):
        raise ValueError("Integration operation failed; check local inputs and CLI compatibility") from None
