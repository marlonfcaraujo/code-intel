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
            "Use exact identifiers or 2-4 distinctive keywords for lookup; it is lexical, not semantic search. "
            "Other multiword queries use function BM25 (legacy catalogs use bounded keyword recovery). "
            "Prefer context_pack for declaration/docstring/body "
            "context; inspect no-match guidance before falling back to broad reads.",
            "Use code-intel for self-contained repository-aware navigation before non-trivial code changes.",
            "Prefer the MCP server tools when configured: `catalog_health`, `catalog_repo`, `workspace_catalog`, "
            "`list_workspaces`, "
            "`workspace_lookup`, `workspace_references`, `workspace_context`, `workspace_context_many`, "
            "`workflow_benchmark`, "
            "`context_pack`, `lookup`, "
            "`find_symbols`, `find_references`, `search_text`, `get_file_outline`, `get_file_content`, "
            "`get_file_tree`, `repo_outline`, `workspace_outline`, `explain_file`, `related_tests`, `risk_report`, "
            "and `savings_report`.",
            f"CLI fallback: refresh with `{command} scan . --incremental --skip-unchanged-meta --json`, then use "
            f"`{command} lookup --repo . query`, `{command} workspace-lookup --workspace name query --source-first`, "
            f"`{command} lookup --repo . KnownSymbol --source-first`, "
            f"`{command} references --repo . SymbolName`, "
            f"`{command} workspace-references --workspace name SymbolName`, "
            f"`{command} workspace-context --workspace name query --source-first`, "
            f"`{command} workflow-benchmark --workspace name --query query --source-first --json --summary`, "
            f"`{command} tree --repo .`, `{command} repo-outline --repo .`, "
            f"`{command} workspace-outline --workspace name`, "
            f"`{command} explain --repo . path/to/file.py`, `{command} find --repo . SymbolName`, "
            f"or `{command} search-text --repo . query`; "
            f"save repeated backend/UI sets with `{command} workspace-save name --repo backend --repo ui/src` "
            f"and refresh them with `{command} workspace-scan --workspace name --incremental "
            "--skip-unchanged-meta --json`; "
            f"use `{command} outline --repo . path/to/file.py` before reading source; "
            f"check catalog freshness with `{command} doctor . --summary` "
            f"and impact with `{command} savings --repo .`.",
            f"Use `{command} install-refresh-job ...` on macOS when a repo needs scheduled incremental indexing.",
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
