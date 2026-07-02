from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from code_intel.benchmark import run_benchmark, run_workflow_benchmark, run_workspace_benchmark
from code_intel.cataloger import build_catalog
from code_intel.cli import main


def test_run_benchmark_compares_catalog_and_jcodemunch(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    report = run_benchmark(
        repo,
        queries=["Service"],
        providers=("catalog", "jcodemunch"),
        repeat=2,
        warmup=0,
        limit=5,
    )

    assert report.repo_path == str(repo.resolve())
    assert [health.provider for health in report.health] == ["catalog", "jcodemunch"]
    assert all(health.available for health in report.health)
    jcodemunch_health = report.health[1]
    assert "x" * 1000 not in json.dumps(jcodemunch_health.details)
    assert "context_metadata" in jcodemunch_health.details["omitted_meta_keys"]
    assert jcodemunch_health.details["meta_key_count"] >= 1
    comparison = report.queries[0]
    assert comparison.query == "Service"
    assert [run.provider for run in comparison.runs] == ["catalog", "jcodemunch"]
    assert all(run.result_count >= 1 for run in comparison.runs)
    assert comparison.runs[0].source_path_count == 1
    assert comparison.runs[0].test_path_count == 1
    assert comparison.runs[0].first_result_path == "src/app/service.py"
    assert comparison.runs[0].first_result_is_test is False
    assert comparison.overlap["jcodemunch"]["shared_path_count"] == 1


def test_run_benchmark_reports_source_and_test_path_quality(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    build_catalog(repo)

    report = run_benchmark(repo, queries=["VALUE_CONFIGS"], providers=("catalog",), repeat=1, warmup=0, limit=5)
    run = report.queries[0].runs[0]

    assert run.source_path_count == 1
    assert run.test_path_count == 1
    assert run.first_result_kind == "constant"
    assert run.first_result_path == "src/app/constants.py"
    assert run.first_result_is_test is False


def test_run_benchmark_supports_lookup_and_text_modes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    lookup_report = run_benchmark(repo, queries=["Service"], mode="lookup", repeat=1, warmup=0, limit=5)
    text_report = run_benchmark(repo, queries=["return 'ok'"], mode="text", repeat=1, warmup=0, limit=5)

    assert lookup_report.mode == "lookup"
    assert lookup_report.providers == ["catalog"]
    assert lookup_report.queries[0].runs[0].mode == "lookup"
    assert lookup_report.queries[0].runs[0].result_count >= 2
    assert text_report.mode == "text"
    assert text_report.providers == ["catalog"]
    assert text_report.queries[0].runs[0].selected_paths == ["src/app/service.py"]


def test_cli_benchmark_outputs_json(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        main(
            [
                "benchmark",
                "--repo",
                str(repo),
                "--query",
                "Service",
                "--provider",
                "catalog",
                "--provider",
                "jcodemunch",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--mode",
                "symbol",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "symbol"
    assert payload["providers"] == ["catalog", "jcodemunch"]
    assert payload["queries"][0]["query"] == "Service"
    assert payload["queries"][0]["runs"][0]["provider"] == "catalog"


def test_cli_benchmark_summary_outputs_compact_json(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        main(
            [
                "benchmark",
                "--repo",
                str(repo),
                "--query",
                "Service",
                "--provider",
                "catalog",
                "--provider",
                "jcodemunch",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--json",
                "--summary",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    run = payload["queries"][0]["runs"][0]
    assert run["provider"] == "catalog"
    assert "top_paths" in run
    assert "source_path_count" in run
    assert "test_path_count" in run
    assert run["first_result_path"] == "src/app/service.py"
    assert run["first_result_is_test"] is False
    assert "top_results" not in run
    assert payload["queries"][0]["overlap"]["jcodemunch"]["shared_path_count"] == 1


def test_run_workspace_benchmark_searches_multiple_catalogs(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    report = run_workspace_benchmark(
        [backend, ui],
        queries=["SurfacePanel"],
        repeat=1,
        warmup=0,
        limit=5,
    )

    assert report.mode == "workspace-lookup"
    assert report.repo_paths == [str(backend.resolve()), str(ui.resolve())]
    assert [health.provider for health in report.health] == ["repo", "ui"]
    run = report.queries[0].runs[0]
    assert run.provider == "workspace"
    assert run.result_count >= 1
    assert run.selected_paths[0] == "ui:components/SurfacePanel.jsx"


def test_cli_workspace_benchmark_outputs_json(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "workspace-benchmark",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "--query",
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

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "workspace-lookup"
    assert payload["queries"][0]["runs"][0]["provider"] == "workspace"
    assert payload["queries"][0]["runs"][0]["selected_paths"][0] == "ui:components/SurfacePanel.jsx"


def test_cli_workspace_benchmark_summary_outputs_compact_json(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "workspace-benchmark",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "--query",
                "SurfacePanel",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--json",
                "--summary",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    run = payload["queries"][0]["runs"][0]
    assert run["provider"] == "workspace"
    assert run["top_paths"] == ["ui:components/SurfacePanel.jsx"]
    assert run["source_path_count"] == 1
    assert run["test_path_count"] == 0
    assert run["first_result_path"] == "ui:components/SurfacePanel.jsx"
    assert "top_results" not in run


def test_cli_workspace_benchmark_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    workspace_dir = tmp_path / "workspaces"
    build_catalog(backend)
    build_catalog(ui)

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
                "workspace-benchmark",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "--query",
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

    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_paths"] == [str(backend.resolve()), str(ui.resolve())]
    assert payload["queries"][0]["runs"][0]["selected_paths"][0] == "ui:components/SurfacePanel.jsx"


def test_run_workflow_benchmark_measures_context_payload(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    report = run_workflow_benchmark(
        [backend, ui],
        queries=["SurfacePanel"],
        repeat=1,
        warmup=0,
        limit=5,
        max_files=2,
        max_lines_per_file=20,
    )

    assert report.mode == "workspace-context"
    assert report.repo_paths == [str(backend.resolve()), str(ui.resolve())]
    assert [health.provider for health in report.health] == ["repo", "ui"]
    run = report.queries[0]
    assert run.query == "SurfacePanel"
    assert run.hit_count >= 1
    assert run.selected_files == 1
    assert run.selected_lines >= 3
    assert run.estimated_tokens > 0
    assert run.estimated_saved_tokens > 0
    assert run.payload_bytes > 0
    assert run.selected_paths == ["ui:components/SurfacePanel.jsx"]
    assert run.top_hits[0]["path"] == "components/SurfacePanel.jsx"


def test_run_workflow_benchmark_can_exclude_test_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    build_catalog(repo)

    report = run_workflow_benchmark(
        [repo],
        queries=["VALUE_CONFIGS"],
        repeat=1,
        warmup=0,
        limit=5,
        max_files=2,
        text_limit=0,
        include_tests=False,
    )

    run = report.queries[0]
    assert report.include_tests is False
    assert run.selected_paths == ["src/app/constants.py"]
    assert run.selected_files == 1


def test_run_workflow_benchmark_can_fallback_to_text_for_ui_labels(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    (ui / "config").mkdir(parents=True)
    (ui / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    build_catalog(ui)

    report = run_workflow_benchmark(
        [ui],
        queries=["Hardware Capacity Planner"],
        repeat=1,
        warmup=0,
        limit=5,
        max_files=1,
        text_limit=0,
        fallback_text_limit=5,
        include_tests=False,
    )

    run = report.queries[0]
    assert run.selected_paths == ["config/navigation.js"]
    assert run.top_hits[0]["kind"] == "text"
    assert run.source_path_count == 1
    assert run.test_path_count == 0


def test_cli_workflow_benchmark_outputs_json(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "workflow-benchmark",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "--query",
                "SurfacePanel",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--source-first",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    run = payload["queries"][0]
    assert payload["mode"] == "workspace-context"
    assert payload["include_tests"] is False
    assert run["query"] == "SurfacePanel"
    assert run["selected_paths"] == ["ui:components/SurfacePanel.jsx"]
    assert run["payload_bytes"] > 0


def test_cli_workflow_benchmark_summary_outputs_compact_json(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "workflow-benchmark",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "--query",
                "SurfacePanel",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--json",
                "--summary",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    run = payload["queries"][0]
    assert run["query"] == "SurfacePanel"
    assert run["top_paths"] == ["ui:components/SurfacePanel.jsx"]
    assert run["source_path_count"] == 1
    assert run["test_path_count"] == 0
    assert run["first_result_path"] == "ui:components/SurfacePanel.jsx"
    assert "top_hits" not in run
    assert "selected_paths" not in run


def test_cli_workflow_benchmark_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    workspace_dir = tmp_path / "workspaces"
    build_catalog(backend)
    build_catalog(ui)

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
                "workflow-benchmark",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "--query",
                "SurfacePanel",
                "--repeat",
                "1",
                "--warmup",
                "0",
                "--json",
                "--summary",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_paths"] == [str(backend.resolve()), str(ui.resolve())]
    assert payload["queries"][0]["top_paths"] == ["ui:components/SurfacePanel.jsx"]


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/__init__.py").write_text("")
    (repo / "src/app/service.py").write_text("class Service:\n    def run(self) -> str:\n        return 'ok'\n")
    (repo / "tests/test_service.py").write_text(
        "from src.app.service import Service\n\ndef test_service() -> None:\n    assert Service().run() == 'ok'\n"
    )
    return repo


def _make_ui_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ui"
    (repo / "components").mkdir(parents=True)
    (repo / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    return repo


def _make_jcodemunch_database(home: Path, repo: Path) -> Path:
    db_dir = home / ".code-index"
    db_dir.mkdir()
    db_path = db_dir / "repo.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE files (
                path TEXT PRIMARY KEY
            );

            CREATE TABLE symbols (
                id TEXT PRIMARY KEY,
                file TEXT,
                name TEXT,
                kind TEXT,
                signature TEXT,
                summary TEXT,
                docstring TEXT,
                line INTEGER,
                qualified_name TEXT
            );
            """
        )
        connection.execute("INSERT INTO meta(key, value) VALUES ('repo', 'local/repo')")
        connection.execute("INSERT INTO meta(key, value) VALUES ('source_root', ?)", (str(repo.resolve()),))
        connection.execute("INSERT INTO meta(key, value) VALUES ('indexed_at', '2026-07-02T00:00:00')")
        connection.execute("INSERT INTO meta(key, value) VALUES ('context_metadata', ?)", ("x" * 1000,))
        connection.execute("INSERT INTO files(path) VALUES ('src/app/service.py')")
        connection.execute(
            """
            INSERT INTO symbols(
                id, file, name, kind, signature, summary, docstring, line, qualified_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "src/app/service.py:Service",
                "src/app/service.py",
                "Service",
                "class",
                "class Service",
                "Runs service work.",
                "Service docs",
                1,
                "src.app.service.Service",
            ),
        )
    return db_path
