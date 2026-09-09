"""Aggregate paired task measurements without exporting identifying metadata."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from statistics import median


def _count(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Usage counts must be nonnegative integers or null")
    return value


def normalize_usage(provider: str, usage: dict) -> dict[str, int | None]:
    """Normalize raw usage, including cache reads in total input tokens.

    Missing fields remain unknown. Anthropic input excludes cache reads and
    writes; OpenAI input includes cached input. Raw records remain in the
    caller-owned input file and are never included in public reports.
    """
    if provider == "normalized":
        input_tokens = _count(usage.get("input_tokens"))
        output_tokens = _count(usage.get("output_tokens"))
        cached = _count(usage.get("cached_input_tokens"))
        written = _count(usage.get("cache_write_tokens"))
    elif provider == "openai":
        input_tokens = _count(usage.get("input_tokens", usage.get("prompt_tokens")))
        output_tokens = _count(usage.get("output_tokens", usage.get("completion_tokens")))
        details = usage.get("input_tokens_details", usage.get("prompt_tokens_details")) or {}
        cached = _count(details.get("cached_tokens"))
        written = None
    elif provider == "anthropic":
        base = _count(usage.get("input_tokens"))
        cached = _count(usage.get("cache_read_input_tokens"))
        written = _count(usage.get("cache_creation_input_tokens"))
        input_tokens = None if None in (base, cached, written) else base + cached + written
        output_tokens = _count(usage.get("output_tokens"))
    else:
        raise ValueError("Unsupported usage provider")
    if input_tokens is not None and cached is not None and cached > input_tokens:
        raise ValueError("Cached input exceeds total input")
    return dict(
        input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached, cache_write_tokens=written
    )


def measured_report(records: list[dict]) -> dict:
    """Compare complete paired tasks with matching experimental controls.

    Records identify a pair with pair_id and an arm with baseline or code_intel.
    Each task includes all model calls, provider usage, success, elapsed_ms,
    and identical model, revision, prompt_id, and config_id controls.
    Only aggregate numbers and fixed labels are returned for publication.
    """
    pairs: dict[str, dict[str, dict]] = {}
    for record in records:
        pair_id = record["pair_id"]
        arm = record["arm"]
        if not isinstance(pair_id, str) or not pair_id or arm not in ("baseline", "code_intel"):
            raise ValueError("Invalid pair or arm")
        pair = pairs.setdefault(pair_id, {})
        if arm in pair:
            raise ValueError("Duplicate task arm")
        if type(record["success"]) is not bool:
            raise ValueError("Task success must be a boolean")
        _count(record["elapsed_ms"])
        if record["elapsed_ms"] is None or not record["calls"]:
            raise ValueError("Elapsed time and model calls are required")
        pair[arm] = record
    if not pairs:
        raise ValueError("At least one paired task is required")
    totals = {arm: [] for arm in ("baseline", "code_intel")}
    for pair in pairs.values():
        if set(pair) != set(totals):
            raise ValueError("Incomplete task pair")
        for key in ("model", "revision", "prompt_id", "config_id"):
            if not pair["baseline"].get(key) or pair["baseline"][key] != pair["code_intel"].get(key):
                raise ValueError("Mismatched or missing experimental controls")
        for arm, record in pair.items():
            calls = [normalize_usage(call["provider"], call["usage"]) for call in record["calls"]]
            measurements = {}
            for key in calls[0]:
                values = [call[key] for call in calls]
                measurements[key] = None if None in values else sum(values)
            counts = [_count(call.get("model_calls", 1)) for call in record["calls"]]
            measurements.update(
                elapsed_ms=record["elapsed_ms"],
                model_calls=None if None in counts else sum(counts),
                success=int(record["success"]),
            )
            totals[arm].append(measurements)
    report = {
        "schema_version": 1,
        "paired_tasks": len(pairs),
        "arms": {},
        "median_paired_reduction_percent": {},
        "experimental_controls": "caller_supplied",
        "tool_policy_independently_verified": False,
    }
    for arm, tasks in totals.items():
        report["arms"][arm] = {}
        for key in tasks[0]:
            values = [task[key] for task in tasks]
            report["arms"][arm][key] = {
                "total": None if None in values else sum(values),
                "available_tasks": sum(value is not None for value in values),
            }
    for key in ("input_tokens", "output_tokens", "elapsed_ms"):
        reductions = []
        for before, after in zip(totals["baseline"], totals["code_intel"], strict=True):
            if before[key] and after[key] is not None:
                reductions.append(100 * (before[key] - after[key]) / before[key])
        report["median_paired_reduction_percent"][key] = {
            "value": median(reductions) if reductions else None,
            "available_pairs": len(reductions),
        }
    return report


def register_measured_usage(subparsers: argparse._SubParsersAction) -> None:
    """Register a privacy-preserving usage report command."""
    parser = subparsers.add_parser("measured-report", help="Compare paired task usage JSONL; emit anonymous JSON")
    parser.add_argument("records", type=Path, help="Local JSONL task records with raw provider usage")
    parser.set_defaults(func=_command)
    runner = subparsers.add_parser("measured-run", help="Run paired tasks through a local agent adapter")
    runner.add_argument("manifest", type=Path)
    runner.add_argument("--repeat", type=int, default=3)
    runner.add_argument("--timeout", type=int, default=300)
    runner.set_defaults(func=_run_command)


def run_measured_tasks(manifest: dict, repeat: int, timeout: int) -> dict:
    """Execute an explicit local adapter with alternating task arms.

    The adapter receives JSON on stdin and returns one task record on stdout.
    It must isolate each task, restrict tools according to the supplied arm,
    validate task success, and collect every provider call's raw usage.
    No raw adapter output is persisted or exposed by this runner.
    """
    command = manifest["adapter"]
    native = isinstance(command, str)
    if not native and (
        not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command)
    ):
        raise ValueError("Adapter must be a nonempty argument array")
    if repeat < 1 or timeout < 1 or not manifest["tasks"]:
        raise ValueError("Positive repeat, timeout and tasks are required")
    records = []
    for iteration in range(repeat):
        for index, task in enumerate(manifest["tasks"]):
            arms = ["baseline", "code_intel"] if (iteration + index) % 2 == 0 else ["code_intel", "baseline"]
            for arm in arms:
                request = dict(task=task, arm=arm, repetition=iteration, controls=manifest["controls"], timeout=timeout)
                started = time.monotonic()
                if native:
                    from code_intel.agent_execution import execute_agent
                    from code_intel.config_upgrade import read_config
                    from code_intel.integrations import AGENTS

                    if command not in AGENTS:
                        raise ValueError("Unknown native adapter")
                    config_dir = Path(manifest.get("integration_dir", Path.home() / ".code-intel" / "integrations"))
                    config = read_config(config_dir / f"{command}.json", "integrations")
                    if not config["enabled"]:
                        raise ValueError("Integration is disabled")
                    request["timeout"] = min(timeout, config["timeout_seconds"])
                    record = execute_agent(command, config["executable"], request)
                else:
                    process = subprocess.run(
                        command, input=json.dumps(request), capture_output=True, text=True, timeout=timeout, check=False
                    )
                    if process.returncode:
                        raise ValueError("Adapter failed; no comparison report generated")
                    record = json.loads(process.stdout)
                for key, value in manifest["controls"].items():
                    if record.get(key) != value:
                        raise ValueError("Adapter controls do not match manifest")
                record.update(
                    pair_id=f"{iteration}:{index}", arm=arm, elapsed_ms=round((time.monotonic() - started) * 1000)
                )
                records.append(record)
    return measured_report(records)


def _run_command(args: argparse.Namespace) -> int:
    try:
        manifest = json.loads(args.manifest.read_text())
        print(json.dumps(run_measured_tasks(manifest, args.repeat, args.timeout), indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.TimeoutExpired):
        raise ValueError("Measurement adapter failed or returned invalid records; private details suppressed") from None


def _command(args: argparse.Namespace) -> int:
    try:
        records = [json.loads(line) for line in args.records.read_text().splitlines() if line.strip()]
        print(json.dumps(measured_report(records), indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        # Do not leak private paths, record content, or provider payloads in errors.
        raise ValueError("Invalid local measurement records; check the documented schema") from None
