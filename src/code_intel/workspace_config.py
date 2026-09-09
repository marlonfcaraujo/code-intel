"""Named workspace configuration for repeated multi-repository operations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from code_intel.config_upgrade import migrate_config, update_config

WORKSPACE_CONFIG_DIRNAME = "workspaces"


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Named group of repository roots used by workspace commands.

    Attributes:
        name: Stable workspace name.
        repos: Absolute repository roots included in the workspace.
        path: Configuration file path backing the workspace.
    """

    name: str
    repos: list[str]
    path: str


def default_workspace_config_dir() -> Path:
    """Return the default directory for named workspace files.

    Returns:
        Path under the user's home directory where workspace definitions live.
    """
    return Path.home() / ".code-intel" / WORKSPACE_CONFIG_DIRNAME


def save_workspace_config(
    name: str,
    repos: list[str | Path],
    *,
    config_dir: str | Path | None = None,
) -> WorkspaceConfig:
    """Persist a named workspace.

    Args:
        name: Workspace name. Names may contain letters, numbers, ``.``, ``-``,
            and ``_``.
        repos: Repository roots to include in the workspace.
        config_dir: Optional directory for workspace JSON files.

    Returns:
        Saved workspace configuration.

    Raises:
        ValueError: If the name or repository list is empty.
        FileNotFoundError: If a repository path does not exist.
        NotADirectoryError: If a repository path is not a directory.
    """
    workspace_name = normalize_workspace_name(name)
    repo_paths = _resolve_repo_paths(repos)
    directory = _workspace_config_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = workspace_config_path(workspace_name, config_dir=directory)
    config = WorkspaceConfig(name=workspace_name, repos=[str(repo_path) for repo_path in repo_paths], path=str(path))
    update_config(path, "workspaces", {"name": config.name, "repos": config.repos})
    return config


def load_workspace_config(name: str, *, config_dir: str | Path | None = None) -> WorkspaceConfig:
    """Load a named workspace.

    Args:
        name: Workspace name.
        config_dir: Optional directory for workspace JSON files.

    Returns:
        Workspace configuration from disk.

    Raises:
        FileNotFoundError: If the named workspace file does not exist.
        ValueError: If the workspace file is malformed.
    """
    workspace_name = normalize_workspace_name(name)
    path = workspace_config_path(workspace_name, config_dir=config_dir)
    if not path.exists():
        raise FileNotFoundError(f"Workspace is not configured: {workspace_name} ({path})")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Workspace configuration is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Workspace configuration must be a JSON object: {path}")
    payload = migrate_config(payload, "workspaces")
    repos = payload.get("repos")
    if not isinstance(repos, list) or not repos or not all(isinstance(repo, str) for repo in repos):
        raise ValueError(f"Workspace configuration must contain a non-empty string repos list: {path}")
    payload_name = payload.get("name", workspace_name)
    if not isinstance(payload_name, str):
        raise ValueError(f"Workspace configuration name must be a string: {path}")
    return WorkspaceConfig(name=normalize_workspace_name(payload_name), repos=repos, path=str(path))


def list_workspace_configs(*, config_dir: str | Path | None = None) -> list[WorkspaceConfig]:
    """List configured workspaces.

    Args:
        config_dir: Optional directory for workspace JSON files.

    Returns:
        Workspace configurations sorted by name.
    """
    directory = _workspace_config_dir(config_dir)
    if not directory.exists():
        return []
    configs = [load_workspace_config(path.stem, config_dir=directory) for path in directory.glob("*.json")]
    return sorted(configs, key=lambda config: config.name)


def resolve_workspace_repos(
    repos: list[str | Path] | None = None,
    *,
    workspace_name: str | None = None,
    config_dir: str | Path | None = None,
) -> list[str]:
    """Resolve repositories from explicit paths and an optional named workspace.

    Args:
        repos: Explicit repository paths supplied by the caller.
        workspace_name: Optional saved workspace to include first.
        config_dir: Optional directory for workspace JSON files.

    Returns:
        Absolute repository paths with duplicates removed in order.

    Raises:
        ValueError: If neither explicit repositories nor a workspace is supplied.
        FileNotFoundError: If the named workspace does not exist.
    """
    ordered: list[str | Path] = []
    if workspace_name:
        ordered.extend(load_workspace_config(workspace_name, config_dir=config_dir).repos)
    if repos:
        ordered.extend(repos)
    if not ordered:
        raise ValueError("at least one repository path or --workspace is required")

    resolved: list[str] = []
    seen: set[str] = set()
    for repo in ordered:
        repo_path = str(Path(repo).expanduser().resolve())
        if repo_path not in seen:
            seen.add(repo_path)
            resolved.append(repo_path)
    return resolved


def workspace_config_to_dict(config: WorkspaceConfig) -> dict[str, Any]:
    """Serialize a workspace configuration.

    Args:
        config: Workspace configuration.

    Returns:
        JSON-serializable dictionary.
    """
    return asdict(config)


def workspace_config_path(name: str, *, config_dir: str | Path | None = None) -> Path:
    """Return the file path for a named workspace.

    Args:
        name: Workspace name.
        config_dir: Optional directory for workspace JSON files.

    Returns:
        Path to the workspace JSON file.
    """
    workspace_name = normalize_workspace_name(name)
    return _workspace_config_dir(config_dir) / f"{workspace_name}.json"


def normalize_workspace_name(name: str) -> str:
    """Validate and normalize a workspace name.

    Args:
        name: User-supplied workspace name.

    Returns:
        Stripped workspace name.

    Raises:
        ValueError: If the name is empty or contains unsafe path characters.
    """
    normalized = name.strip()
    if not normalized:
        raise ValueError("workspace name must not be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(character not in allowed for character in normalized):
        raise ValueError("workspace name may only contain letters, numbers, '.', '-', and '_'")
    return normalized


def _workspace_config_dir(config_dir: str | Path | None) -> Path:
    return Path(config_dir).expanduser().resolve() if config_dir else default_workspace_config_dir()


def _resolve_repo_paths(repos: list[str | Path]) -> list[Path]:
    if not repos:
        raise ValueError("at least one repository path is required")
    resolved: list[Path] = []
    for repo in repos:
        path = Path(repo).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {path}")
        resolved.append(path)
    return resolved
