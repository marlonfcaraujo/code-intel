"""Native agent usage readers; raw transcripts never enter exported records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from code_intel.measured_usage import normalize_usage


@dataclass(frozen=True)
class AgentUsage:
    """Task usage from a native CLI, with API-call counts kept separate.

    Attributes:
        calls: Normalized usage entries, possibly aggregate turn/task totals.
        completed: Whether the agent reached its terminal success state.
        answer: Private response text for an independent grader; never exported.
    """

    calls: list[dict[str, Any]]
    completed: bool
    answer: str


def _entry(usage: dict[str, Any], model_calls: int | None = None) -> dict[str, Any]:
    # Validate every numeric field before letting untrusted data into a report.
    values = normalize_usage("normalized", usage)
    if model_calls is not None and (type(model_calls) is not int or model_calls < 0):
        raise ValueError("Invalid API-call count")
    return {"provider": "normalized", "usage": values, "model_calls": model_calls}


def _exclusive(usage: dict[str, Any], input_key: str, output_key: str, read: object, write: object) -> dict[str, Any]:
    return normalize_usage(
        "anthropic",
        {
            "input_tokens": usage.get(input_key),
            "output_tokens": usage.get(output_key),
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
        },
    )


def _events(text: str) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not events or not all(isinstance(event, dict) for event in events):
        raise ValueError("Expected native JSON events")
    return events


def parse_agent_usage(name: str, text: str) -> AgentUsage:
    """Read verified native event shapes without inferring missing usage.

    Args:
        name: Integration name: codex, hermes, paseo, opencode, or claude.
        text: Private native JSON/JSONL output, or Hermes usage-file contents.

    Returns:
        Task usage and private answer for grading.

    Raises:
        ValueError: When output is incomplete, malformed or lacks task totals.
    """
    if name == "codex":
        events = _events(text)
        terminal = [event for event in events if event.get("type") in {"turn.completed", "turn.failed"}]
        if not terminal:
            raise ValueError("Missing terminal Codex event")
        calls = []
        for event in terminal:
            usage = event.get("usage") or {}
            calls.append(
                _entry(
                    {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "cached_input_tokens": usage.get("cached_input_tokens"),
                        "cache_write_tokens": usage.get("cache_write_input_tokens"),
                    }
                )
            )
        messages = [
            event["item"]["text"]
            for event in events
            if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message"
        ]
        # Codex emits progress commentary as agent_message too. The last such
        # item is the final answer; prepending commentary breaks JSON grading.
        answer = messages[-1] if messages else ""
        return AgentUsage(calls, all(event["type"] == "turn.completed" for event in terminal), answer)
    if name == "hermes":
        usage = json.loads(text)
        values = _exclusive(
            usage, "input_tokens", "output_tokens", usage.get("cache_read_tokens"), usage.get("cache_write_tokens")
        )
        return AgentUsage(
            [_entry(values, usage.get("api_calls"))],
            usage.get("completed") is True and usage.get("failed") is not True,
            "",
        )
    if name == "paseo":
        # inspect.LastUsage is a latest snapshot with unknowns coerced to zero.
        # It cannot establish complete task usage, even when a run is completed.
        raise ValueError("Paseo exposes LastUsage, not complete task usage; import the underlying provider stream")
    if name == "opencode":
        events = _events(text)
        finishes = [event["part"] for event in events if event.get("type") == "step_finish"]
        starts = [event for event in events if event.get("type") == "step_start"]
        if not finishes or finishes[-1].get("reason") not in {"stop", "length", "error"}:
            raise ValueError("Missing final OpenCode step; do not report a partial stream")
        if starts and len(starts) != len(finishes):
            raise ValueError("Incomplete OpenCode steps")
        identifiers = [part.get("id") for part in finishes]
        present = [item for item in identifiers if item is not None]
        if len(present) != len(set(present)):
            raise ValueError("Duplicate OpenCode usage events")
        calls = []
        for part in finishes:
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            calls.append(_entry(_exclusive(tokens, "input", "output", cache.get("read"), cache.get("write"))))
        answer = "\n".join(event["part"]["text"] for event in events if event.get("type") == "text")
        completed = finishes[-1]["reason"] == "stop" and not any(event.get("type") == "error" for event in events)
        return AgentUsage(calls, completed, answer)
    if name == "claude":
        events = _events(text)
        results = [event for event in events if event.get("type") == "result"]
        if len(results) != 1:
            raise ValueError("Expected exactly one Claude task result")
        result = results[0]
        values = normalize_usage("anthropic", result.get("usage") or {})
        # result.usage is already aggregated: do not add assistant.message.usage.
        return AgentUsage(
            [_entry(values)],
            result.get("subtype") == "success" and result.get("is_error") is False,
            result.get("result", ""),
        )
    raise ValueError("Unknown integration")
