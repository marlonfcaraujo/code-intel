from __future__ import annotations

import json
import plistlib
import stat
from pathlib import Path

from code_intel.cli import main
from code_intel.refresh_job import build_launchd_refresh_job, write_launchd_refresh_job


def test_build_launchd_refresh_job_generates_script_and_plist(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    project = tmp_path / "code-intel"
    project.mkdir()

    job = build_launchd_refresh_job(
        [repo],
        label="com.example.code-intel",
        interval_minutes=45,
        project_path=project,
        install_dir=tmp_path / "install",
        launch_agents_dir=tmp_path / "agents",
    )

    assert job.interval_seconds == 2700
    assert job.repos == (str(repo.resolve()),)
    assert job.plist["Label"] == "com.example.code-intel"
    assert job.plist["StartInterval"] == 2700
    assert job.plist["ProgramArguments"] == [job.script_path]
    assert "export HOME=" in job.script
    assert "export GIT_OPTIONAL_LOCKS=0" in job.script
    assert 'export UV_CACHE_DIR="${UV_CACHE_DIR:-$XDG_CACHE_HOME/uv}"' in job.script
    assert (
        f"uv run --project {project.resolve()} code-intel workspace-scan --repo {repo.resolve()} "
        "--incremental --skip-unchanged-meta --json"
    ) in job.script


def test_write_launchd_refresh_job_persists_executable_script_and_plist(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    job = build_launchd_refresh_job(
        [repo],
        interval_minutes=60,
        install_dir=tmp_path / "install",
        launch_agents_dir=tmp_path / "agents",
    )

    write_launchd_refresh_job(job)

    script_path = Path(job.script_path)
    plist_path = Path(job.plist_path)
    parsed_plist = plistlib.loads(plist_path.read_bytes())

    assert script_path.exists()
    assert plist_path.exists()
    assert script_path.stat().st_mode & stat.S_IXUSR
    assert parsed_plist["StartInterval"] == 3600
    assert parsed_plist["ProgramArguments"] == [job.script_path]


def test_cli_install_refresh_job_supports_dry_run_json(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)

    assert (
        main(
            [
                "install-refresh-job",
                str(repo),
                "--interval-minutes",
                "30",
                "--install-dir",
                str(tmp_path / "install"),
                "--launch-agents-dir",
                str(tmp_path / "agents"),
                "--dry-run",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Planned code-intel refresh job" in captured.out
    assert str(repo.resolve()) in captured.out
    assert not (tmp_path / "agents" / "com.code-intel.refresh.plist").exists()


def test_cli_install_refresh_job_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path / "backend")
    ui = _make_repo(tmp_path / "ui")
    workspace_dir = tmp_path / "workspaces"

    assert (
        main(
            [
                "workspace-save",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "--repo",
                str(backend),
                "--repo",
                str(ui),
            ]
        )
        == 0
    )
    _ = capsys.readouterr()

    assert (
        main(
            [
                "install-refresh-job",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "--interval-minutes",
                "30",
                "--install-dir",
                str(tmp_path / "install"),
                "--launch-agents-dir",
                str(tmp_path / "agents"),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["repos"] == [str(backend.resolve()), str(ui.resolve())]


def _make_repo(path: Path) -> Path:
    repo = path / "repo" if path.exists() else path
    repo.mkdir()
    return repo
