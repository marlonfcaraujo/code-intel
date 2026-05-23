from __future__ import annotations

from pathlib import Path

from code_intel.agent_notes import NOTE_START, install_agent_notes
from code_intel.cli import main


def test_install_agent_notes_creates_claude_and_agents_files(tmp_path: Path) -> None:
    results = install_agent_notes(tmp_path, command_prefix="uv run code-intel")

    assert [result.action for result in results] == ["create", "create"]
    claude = tmp_path / "CLAUDE.md"
    agents = tmp_path / "AGENTS.md"
    assert "self-contained repository-aware navigation" in claude.read_text()
    assert "catalog_repo" in agents.read_text()
    assert "savings_report" in agents.read_text()
    assert "uv run code-intel scan ." in claude.read_text()


def test_install_agent_notes_updates_existing_managed_block(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    agents = tmp_path / "AGENTS.md"
    claude.write_text("Existing\n")
    agents.write_text(f"{NOTE_START}\nold\n<!-- code-intel-agent-notes:end -->\n")

    first = install_agent_notes(tmp_path, command_prefix="code-intel")
    second = install_agent_notes(tmp_path, command_prefix="ci")

    assert [result.action for result in first] == ["append", "update"]
    assert [result.action for result in second] == ["update", "update"]
    assert claude.read_text().count(NOTE_START) == 1
    assert agents.read_text().count(NOTE_START) == 1
    assert "ci scan ." in claude.read_text()
    assert "related_tests" in claude.read_text()
    assert "old" not in agents.read_text()


def test_install_agent_notes_handles_agents_symlink(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("Project instructions\n")
    (tmp_path / "AGENTS.md").symlink_to(claude)

    results = install_agent_notes(tmp_path)

    assert [result.action for result in results] == ["append", "skipped-same-target"]
    assert claude.read_text().count(NOTE_START) == 1


def test_cli_install_agent_notes_dry_run_does_not_write(tmp_path: Path, capsys) -> None:
    assert main(["install-agent-notes", str(tmp_path), "--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "create:" in captured.out
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
