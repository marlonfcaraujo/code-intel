from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.cli import main
from code_intel.health import assess_catalog_health


def test_assess_catalog_health_reports_fresh_catalog(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    health = assess_catalog_health(repo)

    assert health["catalog_exists"] is True
    assert health["freshness"]["status"] == "fresh"
    assert health["freshness"]["stale"] is False
    assert health["freshness"]["changed_count"] == 0
    assert health["freshness"]["supports_text_index"] is True
    assert health["text_lines"] == 2
    assert health["dependency_summary"]["supports_categories"] is True


def test_assess_catalog_health_reports_changed_cataloged_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    (repo / "src/app.py").write_text("def app() -> str:\n    return 'changed'\n")

    health = assess_catalog_health(repo)

    assert health["freshness"]["status"] == "stale"
    assert "cataloged_files_changed" in health["freshness"]["reasons"]
    assert health["freshness"]["changed_paths"] == ["src/app.py"]


def test_assess_catalog_health_reports_analyzer_version_mismatch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    CatalogStore.for_repo(repo).update_meta({"analyzer_version": "old"})

    health = assess_catalog_health(repo)

    assert health["freshness"]["status"] == "stale"
    assert "catalog_analyzer_version_changed" in health["freshness"]["reasons"]
    assert health["freshness"]["catalog_analyzer_version"] == "old"
    assert health["freshness"]["analyzer_version_matches"] is False


def test_assess_catalog_health_reports_added_source_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    (repo / "src/extra.py").write_text("def extra() -> None:\n    pass\n")

    health = assess_catalog_health(repo)

    assert "source_files_added" in health["freshness"]["reasons"]
    assert health["freshness"]["added_paths"] == ["src/extra.py"]


def test_cli_doctor_includes_freshness(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    assert main(["doctor", str(repo)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["freshness"]["status"] == "fresh"
    assert payload["freshness"]["supports_incremental"] is True
    assert payload["freshness"]["supports_text_index"] is True
    assert payload["text_lines"] == 2
    assert payload["dependency_summary"]["supports_categories"] is True


def test_cli_doctor_summary_omits_verbose_sections(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    assert main(["doctor", str(repo), "--summary", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["freshness"]["status"] == "fresh"
    assert payload["freshness"]["supports_incremental"] is True
    assert payload["files"] == 1
    assert "dependency_summary" not in payload
    assert "git_dirty_paths" not in payload["freshness"]
    assert "recent" not in payload.get("usage", {})


def test_cli_doctor_handles_legacy_catalog_without_fingerprints(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    _make_legacy_catalog(repo)

    assert main(["doctor", str(repo)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["catalog_exists"] is True
    assert payload["dependencies"] == 0
    assert payload["text_lines"] == 0
    assert payload["freshness"]["status"] == "stale"
    assert payload["freshness"]["supports_incremental"] is False
    assert payload["freshness"]["supports_text_index"] is False
    assert "catalog_schema_lacks_fingerprints" in payload["freshness"]["reasons"]
    assert "catalog_schema_lacks_text_index" in payload["freshness"]["reasons"]
    assert payload["freshness"]["changed_count"] == 0


def test_assess_catalog_health_includes_dependency_categories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "assets").mkdir()
    (repo / "src/styles.css").write_text(".panel { color: red; }\n")
    (repo / "assets/logo.png").write_bytes(b"png")
    (repo / "src/view.jsx").write_text(
        "import React from 'react'\n"
        "import './styles.css'\n"
        "import logo from '../assets/logo.png'\n"
        "import missing from './missing'\n"
        "export const View = () => logo || missing || React\n"
    )
    build_catalog(repo)

    health = assess_catalog_health(repo)
    summary = health["dependency_summary"]

    assert summary["total"] == 4
    assert summary["code"] == 1
    assert summary["external"] == 1
    assert summary["asset"] == 1
    assert summary["unresolved"] == 1
    assert summary["external_packages"][0]["target_path"] == "react"
    assert summary["asset_imports"][0]["target_path"] == "assets/logo.png"
    assert summary["unresolved_imports"][0]["target_path"] == "./missing"


def test_assess_catalog_health_reports_missing_catalog(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    health = assess_catalog_health(repo)

    assert health["catalog_exists"] is False
    assert health["freshness"]["status"] == "missing"
    assert health["freshness"]["reasons"] == ["catalog_missing"]


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("def app() -> str:\n    return 'ok'\n")
    return repo


def _make_legacy_catalog(repo: Path) -> None:
    db_path = repo / ".code-intel/catalog.sqlite"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                language TEXT NOT NULL,
                line_count INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL
            );

            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                signature TEXT NOT NULL,
                doc TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO files(path, language, line_count, size_bytes) VALUES (?, ?, ?, ?)",
            ("src/app.py", "python", 2, len((repo / "src/app.py").read_text())),
        )
        connection.execute(
            """
            INSERT INTO symbols(name, qualified_name, kind, path, line, signature, doc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("app", "src.app.app", "function", "src/app.py", 1, "def app() -> str:", ""),
        )
