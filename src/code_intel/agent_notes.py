"""Managed agent-instruction notes for repositories using code-intel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

NOTE_START = "<!-- code-intel-agent-notes:start -->"
NOTE_END = "<!-- code-intel-agent-notes:end -->"
DEFAULT_AGENT_FILES = ("CLAUDE.md", "AGENTS.md")


@dataclass(frozen=True, slots=True)
class AgentNoteResult:
    """Result of updating one agent instruction file."""

    path: str
    action: str
    target: str


def install_agent_notes(
    repo_path: str | Path,
    *,
    command_prefix: str = "code-intel",
    dry_run: bool = False,
) -> list[AgentNoteResult]:
    """Upsert code-intel usage guidance into CLAUDE.md and AGENTS.md.

    Args:
        repo_path: Repository whose agent instruction files should be updated.
        command_prefix: Command agents should run to invoke this tool.
        dry_run: When true, report planned writes without changing files.

    Returns:
        One result per requested instruction file.
    """
    repo_root = Path(repo_path).resolve()
    if not repo_root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {repo_root}")

    section = build_agent_note_section(command_prefix)
    seen_targets: set[Path] = set()
    results: list[AgentNoteResult] = []

    for filename in DEFAULT_AGENT_FILES:
        path = repo_root / filename
        target = path.resolve() if path.exists() or path.is_symlink() else path
        if target in seen_targets:
            results.append(AgentNoteResult(path=str(path), action="skipped-same-target", target=str(target)))
            continue
        seen_targets.add(target)

        action = _planned_action(path)
        if not dry_run:
            _upsert_section(path, section)
        results.append(AgentNoteResult(path=str(path), action=action, target=str(target)))

    return results


def build_agent_note_section(command_prefix: str) -> str:
    """Build the managed code-intel instruction block."""
    command = command_prefix.strip() or "code-intel"
    return "\n".join(
        [
            NOTE_START,
            "## Code Intel",
            "",
            "Use code-intel for repository-aware navigation before non-trivial code changes, similar to jCodemunch.",
            "Prefer the MCP server tools when configured: `catalog_repo`, `find_symbols`, `explain_file`, "
            "`related_tests`, and `risk_report`.",
            f"CLI fallback: refresh with `{command} scan .`, then use `{command} explain --repo . path/to/file.py` "
            f"or `{command} find --repo . SymbolName`.",
            "The generated catalog is `.code-intel/catalog.sqlite`; do not commit it.",
            NOTE_END,
        ]
    )


def _planned_action(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "create"
    try:
        existing = path.read_text()
    except OSError:
        return "replace"
    if NOTE_START in existing and NOTE_END in existing:
        return "update"
    return "append"


def _upsert_section(path: Path, section: str) -> None:
    if path.exists() or path.is_symlink():
        existing = path.read_text()
    else:
        existing = ""

    start = existing.find(NOTE_START)
    end = existing.find(NOTE_END)
    if start != -1 and end != -1 and end >= start:
        updated = existing[:start] + section + existing[end + len(NOTE_END) :]
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + section + "\n"
    else:
        updated = section + "\n"

    path.write_text(updated)
