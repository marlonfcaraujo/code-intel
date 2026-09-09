"""Synthetic native event fixtures for all supported integrations."""

import json

import pytest

from code_intel.usage_adapters import parse_agent_usage


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


def test_codex_turn_is_not_one_model_call():
    result = parse_agent_usage(
        "codex",
        events(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "private answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 70}},
        ),
    )
    assert result.completed
    assert result.calls[0]["usage"]["input_tokens"] == 100
    assert result.calls[0]["usage"]["cached_input_tokens"] == 70
    assert result.calls[0]["model_calls"] is None
    assert "private answer" not in json.dumps(result.calls)


def test_codex_failure_unknowns_and_truncation():
    failed = parse_agent_usage("codex", events({"type": "turn.failed", "error": {"message": "private"}}))
    assert not failed.completed
    assert failed.calls[0]["usage"]["input_tokens"] is None
    with pytest.raises(ValueError):
        parse_agent_usage("codex", events({"type": "turn.started"}))


def test_hermes_excludes_cache_from_native_input():
    report = dict(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=40,
        api_calls=3,
        completed=True,
        failed=False,
        model="private",
    )
    result = parse_agent_usage("hermes", json.dumps(report))
    assert result.calls[0]["usage"]["input_tokens"] == 80
    assert result.calls[0]["model_calls"] == 3
    assert "private" not in json.dumps(result.calls)
    report["failed"] = True
    assert not parse_agent_usage("hermes", json.dumps(report)).completed


def test_paseo_latest_snapshot_is_not_task_total():
    with pytest.raises(ValueError, match="LastUsage"):
        parse_agent_usage("paseo", json.dumps({"LastUsage": {"InputTokens": 10}}))


def test_opencode_cache_and_steps():
    stream = events(
        {"type": "step_start"},
        {
            "type": "step_finish",
            "part": {
                "id": "a",
                "reason": "tool-calls",
                "tokens": {"input": 10, "output": 5, "cache": {"read": 20, "write": 30}},
            },
        },
        {"type": "step_start"},
        {
            "type": "step_finish",
            "part": {
                "id": "b",
                "reason": "stop",
                "tokens": {"input": 15, "output": 10, "cache": {"read": 30, "write": 0}},
            },
        },
    )
    result = parse_agent_usage("opencode", stream)
    assert result.completed
    assert [call["usage"]["input_tokens"] for call in result.calls] == [60, 45]
    assert [call["model_calls"] for call in result.calls] == [None, None]
    with pytest.raises(ValueError):
        parse_agent_usage("opencode", "\n".join(stream.splitlines()[:-1]))
    with pytest.raises(ValueError):
        parse_agent_usage("opencode", stream + "\n" + stream.splitlines()[-1])


def test_claude_uses_result_not_both_result_and_messages():
    usage = dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=20, cache_creation_input_tokens=30)
    stream = events(
        {"type": "assistant", "message": {"usage": usage}},
        {"type": "result", "subtype": "success", "is_error": False, "usage": usage, "result": "private answer"},
    )
    result = parse_agent_usage("claude", stream)
    assert len(result.calls) == 1
    assert result.calls[0]["usage"]["input_tokens"] == 60
    assert result.calls[0]["model_calls"] is None


@pytest.mark.parametrize("name", ["codex", "hermes", "opencode", "claude"])
def test_bad_json(name):
    with pytest.raises(ValueError):
        parse_agent_usage(name, "not json")
