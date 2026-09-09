"""Configuration and execution tests using local synthetic data only."""

import json
import os
import subprocess
import sys

import pytest

from code_intel.agent_execution import execute_agent, run_private
from code_intel.cli import main
from code_intel.integrations import REQUIRED_FLAGS, import_record, private_json, probe_integration


def test_registration_idempotence_and_permissions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("code_intel.integrations.shutil.which", lambda value: "/synthetic/codex")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, " ".join(REQUIRED_FLAGS["codex"]), ""),
    )
    command = ["integrations", "--config-dir", str(tmp_path), "add", "codex"]
    assert main(command) == 0
    path = tmp_path / "codex.json"
    before = path.stat().st_mtime_ns
    assert main(command) == 0
    assert before == path.stat().st_mtime_ns
    assert path.stat().st_mode & 0o777 == 0o600
    assert "/synthetic" not in capsys.readouterr().out


def test_probe_missing_and_paseo(monkeypatch):
    monkeypatch.setattr("code_intel.integrations.shutil.which", lambda value: None)
    assert not probe_integration("codex")["installed"]
    assert not probe_integration("paseo")["task_usage_supported"]


def test_import_keeps_only_allowed_fields():
    metadata = dict(
        pair_id="example",
        arm="baseline",
        model="test",
        revision="abc",
        prompt_id="p",
        config_id="c",
        elapsed_ms=10,
        success=False,
        secret="never export",
    )
    native = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}})
    record = import_record("codex", native, metadata)
    assert not record["success"]
    assert "secret" not in record
    assert record["calls"][0]["model_calls"] is None


def test_import_cli_writes_private_record_and_refuses_overwrite(tmp_path, capsys):
    metadata = tmp_path / "metadata.json"
    native = tmp_path / "native.jsonl"
    output = tmp_path / "record.jsonl"
    metadata.write_text(
        json.dumps(
            dict(
                pair_id="example",
                arm="baseline",
                model="test",
                revision="abc",
                prompt_id="p",
                config_id="c",
                elapsed_ms=10,
                success=True,
            )
        )
    )
    native.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
    command = [
        "integrations",
        "import",
        "codex",
        "--input",
        str(native),
        "--metadata",
        str(metadata),
        "--output",
        str(output),
    ]
    assert main(command) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["calls"][0]["usage"]["input_tokens"] == 10
    before = output.read_bytes()
    assert main(command) == 1
    assert output.read_bytes() == before
    assert str(tmp_path) not in capsys.readouterr().out


def test_private_json_replacement_mode(tmp_path):
    path = tmp_path / "config.json"
    private_json(path, {"a": 1})
    os.chmod(path, 0o644)
    private_json(path, {"a": 2})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"a": 2}


def test_execute_grades_in_disposable_checkout(tmp_path, monkeypatch):
    from code_intel.usage_adapters import AgentUsage

    controls = dict(model="synthetic-model", revision="a" * 40, prompt_id="p", config_id="c")
    calls = []

    def process(command, **kwargs):
        calls.append((command, kwargs))
        output = ""
        if "rev-parse" in command:
            output = controls["revision"]
        if "clone" in command:
            from pathlib import Path

            Path(command[-1]).mkdir()
        if command == ["prepare-fixture"]:
            output = json.dumps({"isolated": True, "args": [], "env": {}})
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("code_intel.agent_execution.run_private", process)
    monkeypatch.setattr(
        "code_intel.agent_execution._native",
        lambda *args: AgentUsage([{"provider": "openai", "usage": {"input_tokens": 10}}], True, "private answer"),
    )
    result = execute_agent(
        "codex",
        "fake-codex",
        {
            "controls": controls,
            "arm": "baseline",
            "task": {
                "repo": str(tmp_path),
                "prompt": "synthetic",
                "prepare": ["prepare-fixture"],
                "verify": ["grader-fixture"],
            },
        },
    )
    assert result["success"]
    assert "private answer" not in json.dumps(result)
    assert calls[-1][0] == ["grader-fixture"]
    assert json.loads(calls[-1][1]["stdin"])["answer"] == "private answer"
    assert not calls[-1][1]["cwd"].exists()


def test_timeout_and_capture(tmp_path):
    result = run_private([sys.executable, "-c", "print('synthetic')"], cwd=tmp_path)
    assert result.stdout.strip() == "synthetic"
    with pytest.raises(subprocess.TimeoutExpired):
        run_private([sys.executable, "-c", "import time; time.sleep(10)"], cwd=tmp_path, timeout=0.05)


def test_errors_do_not_leak_paths(tmp_path, capsys):
    assert main(["integrations", "--config-dir", str(tmp_path / "sensitive"), "execute", "codex"]) == 1
    assert "sensitive" not in capsys.readouterr().err


@pytest.mark.parametrize("name", ["codex", "hermes", "opencode", "claude"])
def test_native_execution_shapes(name, tmp_path, monkeypatch):
    from pathlib import Path

    from code_intel.agent_execution import _native

    def fake(command, **kwargs):
        assert "explicit-model" in command
        assert not any("bypass" in arg for arg in command)
        if name == "codex":
            assert kwargs["stdin"] == "synthetic task"
            assert "--sandbox" in command and "read-only" in command
            output = {"type": "turn.completed", "usage": {"input_tokens": 10}}
        elif name == "hermes":
            path = Path(command[command.index("--usage-file") + 1])
            path.write_text(
                json.dumps(
                    {
                        "input_tokens": 10,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                        "completed": True,
                        "failed": False,
                    }
                )
            )
            return subprocess.CompletedProcess(command, 0, "synthetic answer", "")
        elif name == "opencode":
            output = {
                "type": "step_finish",
                "part": {"reason": "stop", "tokens": {"input": 10, "cache": {"read": 0, "write": 0}}},
            }
        else:
            output = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr("code_intel.agent_execution.run_private", fake)
    result = _native(name, "fake", "explicit-model", "synthetic task", root, 10, {}, [])
    assert result.completed
    assert result.calls[0]["usage"]["input_tokens"] == 10
