"""Synthetic measurement and privacy tests."""

import json

import pytest

from code_intel.measured_usage import measured_report, normalize_usage, run_measured_tasks


def task(arm, tokens):
    return dict(
        pair_id="private-task",
        arm=arm,
        model="private-model",
        revision="private-revision",
        prompt_id="private-prompt",
        config_id="private-config",
        success=True,
        elapsed_ms=100,
        calls=[
            dict(
                provider="openai",
                usage=dict(input_tokens=tokens, output_tokens=10, input_tokens_details=dict(cached_tokens=5)),
            )
        ],
    )


def test_paired_report_is_anonymous():
    report = measured_report([task("baseline", 100), task("code_intel", 50)])
    assert report["median_paired_reduction_percent"]["input_tokens"]["value"] == 50
    assert "private" not in json.dumps(report)
    assert report["arms"]["baseline"]["input_tokens"]["total"] == 100


def test_cache_semantics_and_unknowns():
    result = normalize_usage(
        "anthropic", dict(input_tokens=10, output_tokens=3, cache_read_input_tokens=20, cache_creation_input_tokens=30)
    )
    assert result["input_tokens"] == 60
    assert normalize_usage("anthropic", dict(input_tokens=10))["input_tokens"] is None
    assert normalize_usage("openai", {})["cached_input_tokens"] is None


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_invalid_counts(value):
    with pytest.raises(ValueError):
        normalize_usage("openai", dict(input_tokens=value))


def test_incomplete_and_mismatched_pairs():
    with pytest.raises(ValueError):
        measured_report([task("baseline", 100)])
    other = task("code_intel", 50)
    other["revision"] = "different"
    with pytest.raises(ValueError):
        measured_report([task("baseline", 100), other])


def test_duplicate_and_unknown_usage():
    with pytest.raises(ValueError):
        measured_report([task("baseline", 100), task("baseline", 100)])
    other = task("code_intel", 50)
    other["calls"][0]["usage"] = {}
    report = measured_report([task("baseline", 100), other])
    assert report["arms"]["code_intel"]["input_tokens"]["total"] is None
    assert report["median_paired_reduction_percent"]["input_tokens"]["value"] is None


def test_runner_alternates_and_checks_controls(monkeypatch):
    import subprocess

    arms = []
    controls = {key: task("baseline", 100)[key] for key in ("model", "revision", "prompt_id", "config_id")}

    def adapter(command, **kwargs):
        assert command == ["synthetic-adapter"]
        request = json.loads(kwargs["input"])
        arms.append(request["arm"])
        result = task(request["arm"], 100 if request["arm"] == "baseline" else 50)
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(subprocess, "run", adapter)
    manifest = dict(adapter=["synthetic-adapter"], controls=controls, tasks=[{"id": "example"}])
    report = run_measured_tasks(manifest, 2, 10)
    assert arms == ["baseline", "code_intel", "code_intel", "baseline"]
    assert report["paired_tasks"] == 2
    assert report["median_paired_reduction_percent"]["input_tokens"]["value"] == 50


def test_cli_errors_do_not_expose_private_data(tmp_path, capsys):
    from code_intel.cli import main

    path = tmp_path / "private-record.jsonl"
    path.write_text('{"private": "invalid"}')
    assert main(["measured-report", str(path)]) == 1
    assert str(path) not in capsys.readouterr().err
