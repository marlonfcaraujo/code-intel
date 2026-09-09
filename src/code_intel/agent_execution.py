"""Execute native agents in disposable checkouts with private task grading."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from code_intel.usage_adapters import AgentUsage, parse_agent_usage


def run_private(
    command: list[str], *, cwd: Path, stdin: str = "", timeout: int = 300, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a private child process and terminate its process group on timeout.

    Args:
        command: Argument vector; never shell-interpolated.
        cwd: Working directory.
        stdin: Private task input.
        timeout: Maximum seconds to wait.
        env: Optional environment for the child.

    Returns:
        Captured process result. Callers must not print raw failure output.
    """
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _argv(value: object) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(arg, str) and arg for arg in value):
        raise ValueError("Expected nonempty command argument array")
    return value


def _native(
    name: str, binary: str, model: str, prompt: str, root: Path, timeout: int, env: dict[str, str], extra: list[str]
) -> AgentUsage:
    if name == "codex":
        command = [
            binary,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "--model",
            model,
            *extra,
            "-",
        ]
        result = run_private(command, cwd=root, stdin=prompt, timeout=timeout, env=env)
    elif name == "hermes":
        usage_file = root.parent / "hermes-usage.json"
        command = [
            binary,
            "--ignore-user-config",
            "--ignore-rules",
            "--safe-mode",
            "--model",
            model,
            "--usage-file",
            str(usage_file),
            *extra,
            "-z",
            prompt,
        ]
        result = run_private(command, cwd=root, timeout=timeout, env=env)
        usage = parse_agent_usage(name, usage_file.read_text())
        return AgentUsage(usage.calls, usage.completed and result.returncode == 0, result.stdout)
    elif name == "opencode":
        command = [binary, "run", "--format", "json", "--model", model, *extra]
        result = run_private(command, cwd=root, stdin=prompt, timeout=timeout, env=env)
    elif name == "claude":
        command = [
            binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--permission-mode",
            "dontAsk",
            "--model",
            model,
            *extra,
        ]
        result = run_private(command, cwd=root, stdin=prompt, timeout=timeout, env=env)
    else:
        raise ValueError("Paseo full-task accounting unavailable; execute its underlying provider")
    usage = parse_agent_usage(name, result.stdout)
    return AgentUsage(usage.calls, usage.completed and result.returncode == 0, usage.answer)


def execute_agent(name: str, binary: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run a graded task using an explicit experiment preparation command.

    Preparation is required: it configures tools and isolation for the chosen
    arm and returns native CLI arguments plus environment overrides. It receives
    the request and temporary checkout path on stdin. This boundary permits
    provider-specific configurations without guessing users' authentication.

    Args:
        name: Native adapter name.
        binary: Registered executable path.
        request: Task, arm, controls and optional timeout from measured-run.

    Returns:
        A private task record with measured usage and independent grading.

    Raises:
        ValueError: If prerequisites, preparation or native usage are invalid.
    """
    if name == "paseo":
        raise ValueError("Paseo full-task accounting is unavailable")
    task = request["task"]
    controls = request["controls"]
    arm = request["arm"]
    timeout = request.get("timeout", 300)
    if type(timeout) is not int or timeout < 1 or arm not in {"baseline", "code_intel"}:
        raise ValueError("Invalid timeout or arm")
    for key in ("model", "revision", "prompt_id", "config_id"):
        if not isinstance(controls.get(key), str) or not controls[key]:
            raise ValueError("Missing controls")
    prompt = task["prompt"]
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("Prompt is required")
    prepare, verify = _argv(task["prepare"]), _argv(task["verify"])
    repo = Path(task["repo"]).expanduser().resolve(strict=True)
    revision = run_private(
        ["git", "rev-parse", "--verify", "--end-of-options", controls["revision"] + "^{commit}"],
        cwd=repo,
        timeout=timeout,
    )
    if revision.returncode or revision.stdout.strip() != controls["revision"]:
        raise ValueError("Use an exact local commit hash for revision")
    with tempfile.TemporaryDirectory(prefix="code-intel-task-") as directory:
        root = Path(directory) / "repo"
        clone = run_private(
            ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--", str(repo), str(root)],
            cwd=Path(directory),
            timeout=timeout,
        )
        if clone.returncode:
            raise ValueError("Could not create isolated checkout")
        checkout = run_private(
            ["git", "checkout", "--quiet", "--detach", controls["revision"]], cwd=root, timeout=timeout
        )
        if checkout.returncode:
            raise ValueError("Could not select revision")
        prepared = run_private(prepare, cwd=root, stdin=json.dumps({**request, "checkout": str(root)}), timeout=timeout)
        if prepared.returncode:
            raise ValueError("Experiment preparation failed")
        configuration = json.loads(prepared.stdout)
        # Explicit assertion, not proof of OS sandboxing. Record that limitation.
        if configuration.get("isolated") is not True:
            raise ValueError("Preparation must confirm experiment isolation")
        extra = configuration.get("args", [])
        if extra:
            extra = _argv(extra)
        overrides = configuration.get("env", {})
        if not isinstance(overrides, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()
        ):
            raise ValueError("Invalid preparation environment")
        env = {**os.environ, **overrides}
        started = time.monotonic()
        usage = _native(name, binary, controls["model"], prompt, root, timeout, env, extra)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        graded = run_private(
            verify, cwd=root, stdin=json.dumps({"answer": usage.answer, "completed": usage.completed}), timeout=timeout
        )
        if graded.returncode not in (0, 1):
            raise ValueError("Grader infrastructure failed")
        return {
            **controls,
            "pair_id": str(task.get("id", "task")),
            "arm": arm,
            "success": usage.completed and graded.returncode == 0,
            "elapsed_ms": elapsed_ms,
            "calls": usage.calls,
            "tool_policy_verified": False,
        }


def execute_stdin(name: str, binary: str, *, timeout_seconds: int = 300) -> int:
    """Read a private adapter request and return a private task record."""
    request = json.load(sys.stdin)
    requested_timeout = request.get("timeout", timeout_seconds)
    if type(requested_timeout) is not int or requested_timeout < 1:
        raise ValueError("Invalid task timeout")
    request["timeout"] = min(requested_timeout, timeout_seconds)
    record = execute_agent(name, binary, request)
    print(json.dumps(record, allow_nan=False))
    return 0
