from __future__ import annotations

import json
from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.cli import main
from code_intel.references import find_references, workspace_references
from code_intel.workspace_config import save_workspace_config


def test_find_references_filters_exact_identifier_matches(tmp_path: Path) -> None:
    repo = _make_backend_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = find_references(repo, store, "Service", limit=10, context_lines=0)

    assert result.selected_files == 2
    assert result.estimated_tokens > 0
    assert any(match.kind == "definition" and match.path == "src/app/service.py" for match in result.matches)
    assert any(match.kind == "reference" and match.path == "src/app/api.py" for match in result.matches)
    assert all("ServiceExtra" not in match.content for match in result.matches)


def test_cli_references_outputs_json_and_records_savings(tmp_path: Path, capsys) -> None:
    repo = _make_backend_repo(tmp_path)
    build_catalog(repo)

    assert main(["references", "--repo", str(repo), "Service", "--context", "0", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 3
    assert payload["selected_files"] == 2
    assert payload["matches"][0]["kind"] == "definition"
    assert all("ServiceExtra" not in match["content"] for match in payload["matches"])

    assert main(["savings", "--repo", str(repo), "--json"]) == 0
    savings = json.loads(capsys.readouterr().out)
    assert savings["by_tool"][0]["tool"] == "references"
    assert savings["estimated_saved_tokens"] > 0


def test_cli_references_summary_only_omits_line_matches(tmp_path: Path, capsys) -> None:
    repo = _make_backend_repo(tmp_path)
    build_catalog(repo)

    assert (
        main(
            [
                "references",
                "--repo",
                str(repo),
                "Service",
                "--context",
                "0",
                "--summary-only",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary_only"] is True
    assert payload["matches"] == []
    assert payload["file_summaries"][0]["path"] == "src/app/api.py"
    assert payload["file_summaries"][0]["match_count"] >= 2
    assert payload["summary_tokens"] == payload["estimated_tokens"]
    assert payload["snippet_tokens"] > 0


def test_workspace_references_finds_ui_component_usage(tmp_path: Path) -> None:
    backend = _make_backend_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    result = workspace_references([backend, ui], "SurfacePanel", limit=10, context_lines=0)

    assert result.selected_files == 2
    assert result.matches[0].repo_label == "ui"
    assert result.matches[0].kind == "definition"
    assert {match.path for match in result.matches} == {"components/SurfacePanel.jsx", "pages/Home.jsx"}


def test_cli_workspace_references_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_backend_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    workspace_dir = tmp_path / "workspaces"
    build_catalog(backend)
    build_catalog(ui)
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    assert (
        main(
            [
                "workspace-references",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "SurfacePanel",
                "--context",
                "0",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 2
    assert payload["selected_files"] == 2
    assert payload["matches"][0]["repo_label"] == "ui"
    assert payload["matches"][0]["kind"] == "definition"


def test_cli_workspace_references_summary_only_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_backend_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    workspace_dir = tmp_path / "workspaces"
    build_catalog(backend)
    build_catalog(ui)
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    assert (
        main(
            [
                "workspace-references",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "SurfacePanel",
                "--context",
                "0",
                "--summary-only",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary_only"] is True
    assert payload["matches"] == []
    assert payload["file_summaries"][0]["repo_label"] == "ui"
    assert {summary["path"] for summary in payload["file_summaries"]} == {
        "components/SurfacePanel.jsx",
        "pages/Home.jsx",
    }


def _make_backend_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "backend"
    (repo / "src/app").mkdir(parents=True)
    (repo / "src/app/service.py").write_text(
        "class Service:\n"
        "    def run(self) -> str:\n"
        "        return 'ok'\n"
        "\n"
        "class ServiceExtra:\n"
        "    pass\n"
        "\n"
        "def build_service() -> Service:\n"
        "    return Service()\n"
    )
    (repo / "src/app/api.py").write_text(
        "from .service import Service\n\ndef handle() -> str:\n    return Service().run()\n"
    )
    (repo / "src/app/noise.py").write_text("\n".join(f"NOISE_{index} = {index}" for index in range(120)))
    return repo


def _make_ui_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ui"
    (repo / "components").mkdir(parents=True)
    (repo / "pages").mkdir()
    (repo / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n"
        "  return <section>{children}</section>;\n"
        "}\n"
        "\n"
        "export function SurfacePanelFrame() {\n"
        "  return null;\n"
        "}\n"
    )
    (repo / "pages/Home.jsx").write_text(
        "import { SurfacePanel } from '../components/SurfacePanel'\n"
        "\n"
        "export function Home() {\n"
        "  return <SurfacePanel />;\n"
        "}\n"
    )
    return repo
