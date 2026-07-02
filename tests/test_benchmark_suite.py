from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from code_intel.benchmark_suite import (
    benchmark_suite_run_to_dict,
    list_benchmark_suite_configs,
    load_benchmark_suite_config,
    load_benchmark_suite_history,
    record_benchmark_suite_run,
    run_benchmark_suite,
    save_benchmark_suite_config,
)
from code_intel.cataloger import build_catalog
from code_intel.cli import main


def test_save_load_and_run_benchmark_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _make_backend_repo(tmp_path / "backend")
    ui = _make_ui_repo(tmp_path / "ui")
    suite_dir = tmp_path / "suites"
    build_catalog(backend)
    build_catalog(ui)
    _make_jcodemunch_database(tmp_path, backend)
    monkeypatch.setenv("HOME", str(tmp_path))

    saved = save_benchmark_suite_config(
        "void",
        [backend, ui],
        symbol_repo=backend,
        symbol_queries=["Service"],
        workflow_queries=["SurfacePanel"],
        config_dir=suite_dir,
        repeat=1,
        warmup=0,
        max_files=1,
    )
    loaded = load_benchmark_suite_config("void", config_dir=suite_dir)
    suites = list_benchmark_suite_configs(config_dir=suite_dir)
    run = run_benchmark_suite(loaded, providers=("catalog", "jcodemunch"))
    payload = benchmark_suite_run_to_dict(run, summary=True)
    history_dir = tmp_path / "history"
    first_record = record_benchmark_suite_run(run, history_dir=history_dir)
    second_record = record_benchmark_suite_run(run, history_dir=history_dir)
    history = load_benchmark_suite_history("void", history_dir=history_dir)

    assert saved.name == "void"
    assert loaded.repos == [str(backend.resolve()), str(ui.resolve())]
    assert loaded.symbol_repo == str(backend.resolve())
    assert [suite.name for suite in suites] == ["void"]
    assert payload["suite"]["name"] == "void"
    assert payload["symbol"]["queries"][0]["query"] == "Service"
    assert payload["symbol"]["queries"][0]["runs"][0]["provider"] == "catalog"
    assert payload["workflow"]["queries"][0]["query"] == "SurfacePanel"
    assert payload["workflow"]["queries"][0]["top_paths"] == ["ui:components/SurfacePanel.jsx"]
    assert payload["scorecard"]["symbol"]["providers"]["catalog"]["source_first_count"] == 1
    assert payload["scorecard"]["symbol"]["providers"]["jcodemunch"]["test_first_count"] == 1
    assert payload["scorecard"]["symbol"]["comparisons"][0]["provider"] == "jcodemunch"
    assert payload["scorecard"]["workflow"]["source_first_count"] == 1
    assert payload["scorecard"]["workflow"]["test_first_count"] == 0
    assert first_record.previous_entry is None
    assert first_record.delta is None
    assert second_record.previous_entry is not None
    assert second_record.delta["workflow"]["source_first_delta"] == 0
    assert len(history) == 2
    assert history[-1]["scorecard"]["workflow"]["source_first_count"] == 1


def test_benchmark_suite_rejects_missing_queries(tmp_path: Path) -> None:
    backend = _make_backend_repo(tmp_path / "backend")

    with pytest.raises(ValueError, match="at least one symbol or workflow query"):
        save_benchmark_suite_config("empty", [backend], config_dir=tmp_path / "suites")


def test_cli_benchmark_suite_save_list_and_run(tmp_path: Path, capsys) -> None:
    backend = _make_backend_repo(tmp_path / "backend")
    ui = _make_ui_repo(tmp_path / "ui")
    suite_dir = tmp_path / "suites"
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "benchmark-suite-save",
                "void",
                "--suite-dir",
                str(suite_dir),
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "--symbol-repo",
                str(backend),
                "--symbol-query",
                "Service",
                "--workflow-query",
                "SurfacePanel",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--json",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["name"] == "void"
    assert saved["symbol_queries"] == ["Service"]

    assert main(["benchmark-suite-list", "--suite-dir", str(suite_dir), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["suites"][0]["name"] == "void"

    assert (
        main(
            [
                "benchmark-suite-run",
                "void",
                "--suite-dir",
                str(suite_dir),
                "--history-dir",
                str(tmp_path / "history"),
                "--provider",
                "catalog",
                "--record-history",
                "--json",
                "--summary",
            ]
        )
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert run["providers"] == ["catalog"]
    assert run["history"]["path"].endswith("void.jsonl")
    assert run["history"]["delta"] is None
    assert run["scorecard"]["symbol"]["providers"]["catalog"]["source_first_count"] == 1
    assert run["scorecard"]["workflow"]["source_first_count"] == 1
    assert run["symbol"]["queries"][0]["runs"][0]["first_result_path"] == "src/app/service.py"
    assert run["workflow"]["queries"][0]["first_result_path"] == "ui:components/SurfacePanel.jsx"

    assert (
        main(
            [
                "benchmark-suite-history",
                "void",
                "--history-dir",
                str(tmp_path / "history"),
                "--json",
            ]
        )
        == 0
    )
    history = json.loads(capsys.readouterr().out)
    assert history["count"] == 1
    assert history["history"][0]["scorecard"]["workflow"]["source_first_count"] == 1


def _make_backend_repo(path: Path) -> Path:
    (path / "src/app").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "src/app/service.py").write_text("class Service:\n    def run(self) -> str:\n        return 'ok'\n")
    (path / "tests/test_service.py").write_text("from src.app.service import Service\n")
    return path


def _make_ui_repo(path: Path) -> Path:
    (path / "components").mkdir(parents=True)
    (path / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    return path


def _make_jcodemunch_database(home: Path, repo: Path) -> Path:
    code_index_dir = home / ".code-index"
    code_index_dir.mkdir()
    database_path = code_index_dir / "repo.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (path TEXT PRIMARY KEY);
            CREATE TABLE symbols (
                name TEXT,
                qualified_name TEXT,
                kind TEXT,
                file TEXT,
                line INTEGER,
                signature TEXT,
                summary TEXT,
                docstring TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("source_root", str(repo.resolve())),
                ("indexed_at", "2026-07-02T00:00:00"),
            ],
        )
        connection.execute("INSERT INTO files(path) VALUES (?)", ("tests/test_service.py",))
        connection.execute(
            """
            INSERT INTO symbols(name, qualified_name, kind, file, line, signature, summary, docstring)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Service",
                "TestService",
                "class",
                "tests/test_service.py",
                1,
                "class TestService",
                "test fixture",
                "",
            ),
        )
    return database_path
