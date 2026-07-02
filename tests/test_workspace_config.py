from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_intel.workspace_config import (
    list_workspace_configs,
    load_workspace_config,
    resolve_workspace_repos,
    save_workspace_config,
)


def test_save_load_and_resolve_workspace_config(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path / "backend")
    ui = _make_repo(tmp_path / "ui")
    workspace_dir = tmp_path / "workspaces"

    saved = save_workspace_config("void", [backend, ui], config_dir=workspace_dir)
    loaded = load_workspace_config("void", config_dir=workspace_dir)
    resolved = resolve_workspace_repos([backend], workspace_name="void", config_dir=workspace_dir)

    assert saved.name == "void"
    assert loaded.repos == [str(backend.resolve()), str(ui.resolve())]
    assert resolved == [str(backend.resolve()), str(ui.resolve())]
    assert json.loads((workspace_dir / "void.json").read_text())["repos"] == loaded.repos


def test_list_workspace_configs_sorts_by_name(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path / "backend")
    workspace_dir = tmp_path / "workspaces"
    save_workspace_config("zeta", [backend], config_dir=workspace_dir)
    save_workspace_config("alpha", [backend], config_dir=workspace_dir)

    configs = list_workspace_configs(config_dir=workspace_dir)

    assert [config.name for config in configs] == ["alpha", "zeta"]


def test_workspace_config_rejects_unsafe_names(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")

    with pytest.raises(ValueError, match="workspace name"):
        save_workspace_config("../bad", [repo], config_dir=tmp_path)


def _make_repo(path: Path) -> Path:
    path.mkdir()
    return path
