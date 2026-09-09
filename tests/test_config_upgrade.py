"""Synthetic upgrade fixtures for preservation, rollback and repeatability."""

import json
import sqlite3
from pathlib import Path

import pytest

from code_intel.catalog_store import CatalogStore
from code_intel.catalog_upgrade import upgrade_catalog
from code_intel.cataloger import build_catalog
from code_intel.config_upgrade import migrate_config, read_config, update_config, upgrade_configs
from code_intel.workspace_config import load_workspace_config, save_workspace_config


def write_config(root: Path, kind: str, payload: dict) -> Path:
    path = root / kind / "example.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def test_dry_run_and_repeat_preserve_custom_values(tmp_path):
    path = write_config(
        tmp_path,
        "integrations",
        dict(
            schema_version=1,
            integration="codex",
            executable="/example/codex",
            enabled=False,
            timeout_seconds=42,
            custom={"nested": "keep"},
        ),
    )
    before = path.read_bytes()
    assert upgrade_configs(tmp_path, dry_run=True)["changes"] == 1
    assert path.read_bytes() == before
    assert not (path.parent / ".backups").exists()
    assert not list(path.parent.glob("*.sqlite"))
    assert upgrade_configs(tmp_path)["backups_created"] == 1
    result = json.loads(path.read_text())
    assert result["schema_version"] == 2
    assert result["custom"] == {"nested": "keep"}
    assert result["enabled"] is False and result["timeout_seconds"] == 42
    backup = next((path.parent / ".backups").iterdir())
    assert backup.read_bytes() == before
    assert backup.stat().st_mode & 0o777 == 0o600
    after_mtime = path.stat().st_mtime_ns
    assert upgrade_configs(tmp_path)["changes"] == 0
    assert path.stat().st_mtime_ns == after_mtime


def test_read_supplies_defaults_without_rewriting(tmp_path):
    path = write_config(tmp_path, "integrations", {"integration": "codex", "executable": "example"})
    before = path.read_bytes()
    result = read_config(path, "integrations")
    assert result["enabled"] is True
    assert result["timeout_seconds"] == 300
    assert path.read_bytes() == before


def test_all_files_preflight_before_write(tmp_path):
    good = write_config(tmp_path, "workspaces", {"repos": ["/example"], "name": "example"})
    bad = write_config(tmp_path, "integrations", {"schema_version": 999})
    before = good.read_bytes()
    with pytest.raises(ValueError):
        upgrade_configs(tmp_path)
    assert good.read_bytes() == before
    assert not (good.parent / ".backups").exists()
    assert json.loads(bad.read_text())["schema_version"] == 999


@pytest.mark.parametrize("version", [True, -1, 99, "1"])
def test_reject_invalid_versions(version):
    with pytest.raises(ValueError):
        migrate_config({"schema_version": version, "repos": ["/example"]}, "workspaces")


def test_save_preserves_unknown_workspace_fields(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(tmp_path, "workspaces", {"name": "example", "repos": [str(repo)], "custom": [1, 2]})
    save_workspace_config("example", [repo], config_dir=path.parent)
    assert json.loads(path.read_text())["custom"] == [1, 2]
    assert load_workspace_config("example", config_dir=path.parent).repos == [str(repo)]


def test_atomic_failure_preserves_original(tmp_path, monkeypatch):
    path = write_config(tmp_path, "workspaces", {"repos": ["/example"]})
    before = path.read_bytes()
    import os

    original_replace = os.replace

    def fail_destination(source, destination):
        if Path(destination) == path:
            raise OSError("synthetic replacement failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_destination)
    with pytest.raises(OSError):
        update_config(path, "workspaces")
    assert path.read_bytes() == before
    assert next((path.parent / ".backups").iterdir()).read_bytes() == before


def test_concurrent_edit_and_symlink_rejected(tmp_path):
    path = write_config(tmp_path, "workspaces", {"repos": ["/example"]})
    with pytest.raises(ValueError, match="preflight"):
        update_config(path, "workspaces", expected=b"changed")
    link = path.with_name("link.json")
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        upgrade_configs(tmp_path)


def test_catalog_migration_preserves_history_and_backup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text('def example():\n    """Synthetic example."""\n    return 1\n')
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)
    store.record_usage_event(tool="find", provider="catalog", estimated_saved_tokens=123)
    path = store.database_path
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=0")
    before = path.read_bytes()
    assert upgrade_catalog(path, dry_run=True)["changed"]
    assert path.read_bytes() == before
    assert upgrade_catalog(path)["backup_created"]
    assert store.usage_summary()["estimated_saved_tokens"] == 123
    backup = next((path.parent / ".backups").iterdir())
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
    assert upgrade_catalog(path)["changed"] is False


def test_future_catalog_cannot_be_read_or_replaced(tmp_path):
    (tmp_path / "sample.py").write_text("x = 1\n")
    build_catalog(tmp_path)
    store = CatalogStore.for_repo(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError):
        upgrade_catalog(store.database_path)
    with pytest.raises(ValueError):
        store.connect()
    with pytest.raises(ValueError):
        build_catalog(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
