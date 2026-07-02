"""launchd refresh job generation for recurring catalog updates."""

from __future__ import annotations

import plistlib
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REFRESH_LABEL = "com.code-intel.refresh"
DEFAULT_REFRESH_INTERVAL_MINUTES = 180


@dataclass(frozen=True, slots=True)
class LaunchdRefreshJob:
    """Description of a launchd job that refreshes code-intel catalogs.

    Attributes:
        label: launchd label used for the agent.
        interval_minutes: Refresh cadence in minutes.
        repos: Repository paths refreshed by the job.
        script_path: Shell script path invoked by launchd.
        plist_path: LaunchAgent plist path.
        stdout_path: Standard output log path.
        stderr_path: Standard error log path.
        script: Shell script content.
        plist: launchd property list payload.
    """

    label: str
    interval_minutes: int
    repos: tuple[str, ...]
    script_path: str
    plist_path: str
    stdout_path: str
    stderr_path: str
    script: str
    plist: dict[str, Any]

    @property
    def interval_seconds(self) -> int:
        """Return the launchd StartInterval value in seconds.

        Returns:
            Refresh cadence converted from minutes to seconds.
        """
        return self.interval_minutes * 60

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the refresh job.

        Returns:
            Dictionary containing paths, cadence, repositories, and the plist
            payload used for the launchd job.
        """
        return {
            "label": self.label,
            "interval_minutes": self.interval_minutes,
            "interval_seconds": self.interval_seconds,
            "repos": list(self.repos),
            "script_path": self.script_path,
            "plist_path": self.plist_path,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "plist": self.plist,
        }


def build_launchd_refresh_job(
    repos: list[str | Path],
    *,
    label: str = DEFAULT_REFRESH_LABEL,
    interval_minutes: int = DEFAULT_REFRESH_INTERVAL_MINUTES,
    command: str = "code-intel",
    project_path: str | Path | None = None,
    install_dir: str | Path | None = None,
    launch_agents_dir: str | Path | None = None,
) -> LaunchdRefreshJob:
    """Build a launchd job definition for recurring incremental scans.

    Args:
        repos: Repository roots to refresh with ``code-intel workspace-scan --incremental``.
        label: launchd job label.
        interval_minutes: Refresh cadence in minutes.
        command: Base command used to invoke the code-intel CLI. Ignored as a
            path only when ``project_path`` is provided; in that case it is the
            console script name passed to ``uv run``.
        project_path: Optional source checkout path for ``uv run --project``.
        install_dir: Directory for generated runner scripts and logs.
        launch_agents_dir: Directory for generated LaunchAgent plists.

    Returns:
        Launchd job description with generated script and plist content.

    Raises:
        ValueError: If no repository is supplied or the interval is invalid.
        FileNotFoundError: If a supplied repository path does not exist.
        NotADirectoryError: If a supplied repository path is not a directory.
    """
    repo_paths = _resolve_repo_paths(repos)
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be greater than 0")

    safe_label = _safe_label(label)
    install_root = Path(install_dir).expanduser().resolve() if install_dir else Path.home() / ".code-intel" / "launchd"
    agents_root = (
        Path(launch_agents_dir).expanduser().resolve()
        if launch_agents_dir
        else Path.home() / "Library" / "LaunchAgents"
    )
    log_dir = install_root / "logs"
    script_path = install_root / f"{safe_label}.sh"
    stdout_path = log_dir / f"{safe_label}.out.log"
    stderr_path = log_dir / f"{safe_label}.err.log"
    plist_path = agents_root / f"{safe_label}.plist"
    script = _build_script(repo_paths, command=command, project_path=project_path)
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [str(script_path)],
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return LaunchdRefreshJob(
        label=label,
        interval_minutes=interval_minutes,
        repos=tuple(str(path) for path in repo_paths),
        script_path=str(script_path),
        plist_path=str(plist_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        script=script,
        plist=plist,
    )


def write_launchd_refresh_job(job: LaunchdRefreshJob) -> LaunchdRefreshJob:
    """Write a launchd refresh job script and plist to disk.

    Args:
        job: Refresh job definition to persist.

    Returns:
        The same refresh job after files have been written.
    """
    script_path = Path(job.script_path)
    plist_path = Path(job.plist_path)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    Path(job.stdout_path).parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(job.script)
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    plist_path.write_bytes(plistlib.dumps(job.plist, sort_keys=False))
    return job


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


def _safe_label(label: str) -> str:
    safe = "".join(character if character.isalnum() or character in ".-_" else "_" for character in label.strip())
    return safe or DEFAULT_REFRESH_LABEL


def _build_script(
    repos: list[Path],
    *,
    command: str,
    project_path: str | Path | None,
) -> str:
    base_command = _base_command(command=command, project_path=project_path)
    home = shlex.quote(str(Path.home()))
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"export HOME={home}",
        'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"',
        'export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"',
        'export UV_CACHE_DIR="${UV_CACHE_DIR:-$XDG_CACHE_HOME/uv}"',
        "export GIT_OPTIONAL_LOCKS=0",
        "",
        "timestamp() {",
        "  date '+%Y-%m-%dT%H:%M:%S%z'",
        "}",
        "",
        'echo "[$(timestamp)] starting code-intel refresh"',
    ]
    repo_args = " ".join(f"--repo {shlex.quote(str(repo))}" for repo in repos)
    lines.append(f"{base_command} workspace-scan {repo_args} --incremental --skip-unchanged-meta --json")
    lines.append('echo "[$(timestamp)] refreshed code-intel workspace"')
    lines.append('echo "[$(timestamp)] finished code-intel refresh"')
    lines.append("")
    return "\n".join(lines)


def _base_command(*, command: str, project_path: str | Path | None) -> str:
    if project_path is not None:
        parts = ["uv", "run", "--project", str(Path(project_path).expanduser().resolve()), command]
    else:
        parts = shlex.split(command)
    if not parts:
        raise ValueError("command must not be empty")
    return " ".join(shlex.quote(part) for part in parts)
