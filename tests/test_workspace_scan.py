from __future__ import annotations

import json
from pathlib import Path

from code_intel.cli import main
from code_intel.mcp_server import workspace_catalog_tool
from code_intel.workspace_config import save_workspace_config
from code_intel.workspace_scan import scan_workspace, workspace_scan_to_dict


def test_scan_workspace_refreshes_repos_and_reuses_incremental_catalogs(tmp_path: Path) -> None:
    backend = _make_python_repo(tmp_path / "backend")
    ui = _make_ui_repo(tmp_path / "ui")

    first = scan_workspace([backend, ui], incremental=True, workers=1, repo_workers=1)
    second = scan_workspace(
        [backend, ui],
        incremental=True,
        workers=1,
        repo_workers=2,
        skip_unchanged_meta=True,
    )

    assert first.repo_count == 2
    assert first.scanned_count == 2
    assert first.failed_count == 0
    assert second.repo_workers == 2
    assert all(result.ok for result in second.repos)
    assert all(result.catalog["incremental"] is True for result in second.repos)
    assert all(result.catalog["changed_file_count"] == 0 for result in second.repos)
    assert all(result.catalog["written_file_count"] == 0 for result in second.repos)


def test_scan_workspace_reports_repo_failures_without_stopping(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path / "repo")
    missing = tmp_path / "missing"

    report = scan_workspace([repo, missing], incremental=True, workers=1, repo_workers=1)
    payload = workspace_scan_to_dict(report)

    assert report.scanned_count == 1
    assert report.failed_count == 1
    assert payload["repos"][0]["ok"] is True
    assert payload["repos"][1]["ok"] is False
    assert "FileNotFoundError" in payload["repos"][1]["error"]


def test_cli_workspace_scan_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_python_repo(tmp_path / "backend")
    ui = _make_ui_repo(tmp_path / "ui")
    workspace_dir = tmp_path / "workspaces"
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    assert (
        main(
            [
                "workspace-scan",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "--incremental",
                "--workers",
                "1",
                "--repo-workers",
                "1",
                "--skip-unchanged-meta",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_count"] == 2
    assert payload["scanned_count"] == 2
    assert payload["failed_count"] == 0
    assert [repo["ok"] for repo in payload["repos"]] == [True, True]


def test_mcp_workspace_catalog_uses_named_workspace(tmp_path: Path) -> None:
    backend = _make_python_repo(tmp_path / "backend")
    ui = _make_ui_repo(tmp_path / "ui")
    workspace_dir = tmp_path / "workspaces"
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    result = workspace_catalog_tool(
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        incremental=True,
        workers=1,
        repo_workers=1,
        skip_unchanged_meta=True,
    )

    assert result["repo_count"] == 2
    assert result["scanned_count"] == 2
    assert result["failed_count"] == 0
    assert {repo["catalog"]["file_count"] for repo in result["repos"]} == {1}


def _make_python_repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "src/app.py").write_text("def app() -> str:\n    return 'ok'\n")
    return path


def _make_ui_repo(path: Path) -> Path:
    (path / "components").mkdir(parents=True)
    (path / "components/SurfacePanel.jsx").write_text("export function SurfacePanel() {\n  return <section />;\n}\n")
    return path
