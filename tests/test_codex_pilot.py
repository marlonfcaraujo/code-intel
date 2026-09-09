"""Validate the public pilot's grading and source-only tool boundaries."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

PILOT_PATH = Path(__file__).parents[1] / "benchmarks" / "codex_pilot.py"
SPEC = importlib.util.spec_from_file_location("codex_pilot", PILOT_PATH)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def test_source_boundary_rejects_parent_absolute_and_symlink(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    inside = root / "src" / "example.py"
    inside.write_text("x = 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 'synthetic'\n")
    (root / "src" / "link.py").symlink_to(outside)
    assert pilot.source_path(root, "src/example.py") == inside
    for path in ("../outside.py", str(outside), "src/link.py", ".git/config"):
        with pytest.raises(ValueError):
            pilot.source_path(root, path)


def test_grade_requires_every_expected_value_and_type():
    import json

    answer = dict(pilot.TASKS["integration_defaults"]["expected"])
    assert pilot.grade("integration_defaults", json.dumps(answer))
    answer["enabled_default"] = 1
    assert not pilot.grade("integration_defaults", json.dumps(answer))
    assert not pilot.grade("integration_defaults", "could not inspect source")


def test_tool_surfaces_and_annotations(tmp_path):
    pytest.importorskip("mcp")
    (tmp_path / "src").mkdir()
    for arm, expected in (("baseline", pilot.BASE_TOOLS), ("code_intel", pilot.BASE_TOOLS | pilot.EXTRA_TOOLS)):
        server = pilot.make_server(tmp_path, arm, tmp_path / "audit.jsonl")
        tools = asyncio.run(server.list_tools())
        assert {tool.name for tool in tools} == expected
        assert all(tool.annotations.readOnlyHint and not tool.annotations.openWorldHint for tool in tools)


def test_codex_settings_disable_external_tools_and_scope_approvals(tmp_path):
    command = pilot.codex_arguments(tmp_path, "baseline", tmp_path / "audit", "synthetic-model")
    assert "read-only" in command
    assert "--ignore-user-config" in command
    assert "shell_tool" in command
    assert "plugins" in command
    assert "memories" in command
    assert 'mcp_servers.pilot.tools.read_file.approval_mode="auto"' in command
    assert not any("tools.context_pack.approval_mode" in arg for arg in command)


@pytest.mark.parametrize(
    "filename", ["codex-pilot-2026-09-09.json", "codex-recovery-2026-09-09.json", "pytest-bm25-2026-09-09.json"]
)
def test_published_report_totals_match_per_run_receipts(filename):
    import json

    path = Path(__file__).parents[1] / "docs" / "benchmarks" / filename
    report = json.loads(path.read_text())
    for arm in ("baseline", "code_intel"):
        receipts = [row for row in report["receipts"] if row["arm"] == arm]
        assert len(receipts) == report["paired_tasks"]
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens"):
            assert sum(row["usage"][key] for row in receipts) == report["arms"][arm][key]["total"]
        assert sum(row["elapsed_ms"] for row in receipts) == report["arms"][arm]["elapsed_ms"]["total"]
        assert sum(row["success"] for row in receipts) == report["arms"][arm]["success"]["total"]
    assert len({(row["task"], row["repeat"], row["arm"]) for row in report["receipts"]}) == 12
