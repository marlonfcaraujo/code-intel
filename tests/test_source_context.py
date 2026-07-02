from __future__ import annotations

import json
from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.cli import main
from code_intel.source_context import (
    get_file_content,
    get_file_outline,
    get_file_tree,
    get_repo_outline,
    get_workspace_outline,
)
from code_intel.workspace_config import save_workspace_config


def test_get_file_outline_returns_symbols_without_full_source(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    outline = get_file_outline(repo, store, "service.py")

    assert outline["path"] == "src/app/service.py"
    assert outline["symbol_count"] == 3
    assert [symbol["qualified_name"] for symbol in outline["symbols"]] == ["Service", "Service.run", "helper"]


def test_get_file_content_returns_bounded_source(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    content = get_file_content(repo, store, "src/app/service.py", start_line=2, end_line=3)

    assert content["start_line"] == 2
    assert content["end_line"] == 3
    assert content["content"] == "    def run(self) -> str:\n        return 'ok'"


def test_get_file_tree_returns_catalog_structure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    tree = get_file_tree(repo, store, prefix="src", max_depth=2)

    assert tree["prefix"] == "src"
    assert tree["total_files"] == 2
    assert any(entry["path"] == "src/app" and entry["type"] == "directory" for entry in tree["entries"])
    assert any(entry["path"] == "src/app/service.py" and entry["symbol_count"] == 3 for entry in tree["entries"])


def test_get_repo_outline_returns_language_and_directory_summary(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    outline = get_repo_outline(repo, store, max_depth=2, top_files=3)

    assert outline["total_files"] == 2
    assert outline["total_symbols"] >= 3
    assert outline["languages"][0]["language"] == "python"
    assert any(row["path"] == "src/app" for row in outline["directories"])
    assert outline["top_files"][0]["symbol_count"] >= outline["top_files"][-1]["symbol_count"]


def test_get_workspace_outline_aggregates_backend_and_ui(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    outline = get_workspace_outline([backend, ui], max_depth=2, top_files=2)

    assert outline["repo_count"] == 2
    assert outline["searched_repos"] == 2
    assert outline["total_files"] == 3
    assert {repo["label"] for repo in outline["repos"]} == {"repo", "ui"}
    assert {row["language"] for row in outline["languages"]} == {"python", "jsx"}
    assert outline["top_files"][0]["repo_label"] in {"repo", "ui"}


def test_cli_outline_and_content_json(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    assert main(["outline", "--repo", str(repo), "src/app/service.py", "--json"]) == 0
    outline = json.loads(capsys.readouterr().out)
    assert outline["symbols"][0]["name"] == "Service"

    assert (
        main(
            [
                "content",
                "--repo",
                str(repo),
                "src/app/service.py",
                "--start-line",
                "1",
                "--end-line",
                "1",
                "--json",
            ]
        )
        == 0
    )
    content = json.loads(capsys.readouterr().out)
    assert content["content"] == "class Service:"


def test_cli_tree_and_repo_outline_json(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    assert main(["tree", "--repo", str(repo), "--prefix", "src", "--max-depth", "2", "--json"]) == 0
    tree = json.loads(capsys.readouterr().out)
    assert tree["prefix"] == "src"
    assert any(entry["path"] == "src/app/service.py" for entry in tree["entries"])

    assert main(["repo-outline", "--repo", str(repo), "--json"]) == 0
    outline = json.loads(capsys.readouterr().out)
    assert outline["total_files"] == 2
    assert outline["directories"]


def test_cli_workspace_outline_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    workspace_dir = tmp_path / "workspaces"
    build_catalog(backend)
    build_catalog(ui)
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    assert (
        main(
            [
                "workspace-outline",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "--json",
            ]
        )
        == 0
    )

    outline = json.loads(capsys.readouterr().out)
    assert outline["repo_count"] == 2
    assert outline["searched_repos"] == 2
    assert outline["total_files"] == 3
    assert len(outline["repos"]) == 2


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "src/app/service.py").write_text(
        "class Service:\n"
        "    def run(self) -> str:\n"
        "        return 'ok'\n"
        "\n"
        "def helper() -> str:\n"
        "    return Service().run()\n"
    )
    (repo / "src/app/api.py").write_text("from .service import Service\n\nservice = Service()\n")
    return repo


def _make_ui_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ui"
    (repo / "components").mkdir(parents=True)
    (repo / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    return repo
