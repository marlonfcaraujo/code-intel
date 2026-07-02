from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import code_intel.cataloger as cataloger
from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import CATALOG_ANALYZER_VERSION, build_catalog
from code_intel.change_report import compute_change_report
from code_intel.cli import main
from code_intel.discovery import DiscoveredSourceFile
from code_intel.lookup import lookup
from code_intel.risk import top_risk_files
from code_intel.tests_map import find_related_tests
from code_intel.workspace import workspace_lookup


def test_catalog_find_explain_and_related_tests(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    result = build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    assert result.file_count == 4
    assert result.symbol_count >= 4
    assert result.dependency_count >= 2

    symbols = store.search_symbols("Service")
    assert symbols[0]["path"] == "src/app/service.py"

    report = compute_change_report(repo, store, "src/app/service.py")
    assert report.path == "src/app/service.py"
    assert "src/app/api.py" in report.direct_dependents
    assert "tests/test_service.py" in {match.path for match in report.related_tests}
    assert report.blast_score == 4.5
    assert report.risk == "low"

    related_tests = find_related_tests(repo, store, "src/app/service.py")
    assert related_tests[0].path == "tests/test_service.py"


def test_catalog_store_returns_aggregate_counts(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    result = build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    counts = store.catalog_counts()

    assert counts.file_count == result.file_count
    assert counts.symbol_count == result.symbol_count
    assert counts.dependency_count == result.dependency_count
    assert counts.text_line_count == result.text_line_count


def test_catalog_store_can_reuse_sqlite_connection(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    store = CatalogStore(database_path, reuse_connection=True)

    first = store.connect()
    second = store.connect()

    assert first is second

    store.close()
    third = store.connect()

    assert third is not first
    store.close()


def test_reused_catalog_store_caches_schema_checks(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo, reuse_connection=True)
    connection = store.connect()
    schema_queries: list[str] = []
    connection.set_trace_callback(
        lambda statement: schema_queries.append(statement) if "sqlite_master" in statement else None
    )

    assert store.has_catalog() is True
    first_query_count = len(schema_queries)
    assert first_query_count > 0

    assert store.has_catalog() is True
    assert len(schema_queries) == first_query_count

    store.close()


def test_reused_catalog_store_caches_file_rows_for_path_search(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo, reuse_connection=True)
    connection = store.connect()
    file_load_queries: list[str] = []
    connection.set_trace_callback(
        lambda statement: (
            file_load_queries.append(statement) if "SELECT * FROM files ORDER BY path" in statement else None
        )
    )

    first = store.search_files_many(["service"], limit=5)
    second = store.search_files_many(["service"], limit=5)

    assert [row["path"] for row in first] == ["src/app/service.py", "tests/test_service.py"]
    assert [row["path"] for row in second] == [row["path"] for row in first]
    assert len(file_load_queries) == 1
    store.close()


def test_risk_report_orders_highest_scores_first(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    rows = top_risk_files(repo, store, limit=3)

    assert rows
    assert rows[0].score >= rows[-1].score
    assert any(row.path == "src/app/service.py" for row in rows)


def test_related_tests_do_not_match_generic_client_stems_across_packages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/custom_connectors/acm").mkdir(parents=True)
    (repo / "src/custom_connectors/email").mkdir(parents=True)
    (repo / "tests/acm").mkdir(parents=True)
    (repo / "tests/email").mkdir(parents=True)
    (repo / "src/custom_connectors/acm/client.py").write_text("class ACMClient:\n    pass\n")
    (repo / "src/custom_connectors/email/client.py").write_text("class EmailClient:\n    pass\n")
    (repo / "tests/acm/test_client.py").write_text("from custom_connectors.acm.client import ACMClient\n")
    (repo / "tests/email/test_client.py").write_text("from custom_connectors.email.client import EmailClient\n")
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    related_tests = find_related_tests(repo, store, "src/custom_connectors/acm/client.py")

    assert {match.path for match in related_tests} == {"tests/acm/test_client.py"}


def test_cli_smoke_scan_and_find(tmp_path: Path, capsys) -> None:
    repo = _make_python_repo(tmp_path)

    assert main(["scan", str(repo)]) == 0
    assert main(["find", "--repo", str(repo), "Service"]) == 0

    captured = capsys.readouterr()
    assert "Cataloged 4 files" in captured.out
    assert "src/app/service.py" in captured.out


def test_cli_scan_json_reports_timing_and_reuse_metrics(tmp_path: Path, capsys) -> None:
    repo = _make_python_repo(tmp_path)

    assert main(["scan", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["file_count"] == 4
    assert payload["text_line_count"] > 0
    assert payload["analysis_workers"] == 1
    assert payload["timings_ms"]["total"] >= 0

    assert main(["scan", str(repo), "--incremental", "--workers", "1", "--json"]) == 0
    incremental_payload = json.loads(capsys.readouterr().out)

    assert incremental_payload["incremental"] is True
    assert incremental_payload["reused_file_count"] == 4
    assert incremental_payload["changed_file_count"] == 0
    assert incremental_payload["written_file_count"] == 0
    assert incremental_payload["timings_ms"]["analysis"] == 0.0


def test_catalog_and_cli_search_indexed_text(tmp_path: Path, capsys) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    matches = store.search_text("return 'ok'", limit=5, context_lines=0)

    assert matches[0].path == "src/app/service.py"
    assert matches[0].line == 3
    assert "return 'ok'" in matches[0].content
    assert main(["search-text", "--repo", str(repo), "return 'ok'", "--context", "0"]) == 0
    assert "src/app/service.py:3" in capsys.readouterr().out


def test_catalog_redacts_secret_values_from_text_and_symbol_signatures(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "settings.py").write_text(
        'API_TOKEN = "sk-1234567890abcdefghijklmnopqrstuvwxyz"\ndef token_name() -> str:\n    return API_TOKEN\n'
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    secret_matches = store.search_text("sk-1234567890", limit=5, context_lines=0)
    token_matches = store.search_text("API_TOKEN", limit=5, context_lines=0)
    symbols = store.search_symbols("API_TOKEN")

    assert secret_matches == []
    assert token_matches
    assert "sk-1234567890" not in token_matches[0].content
    assert "[REDACTED_SECRET]" in token_matches[0].content
    assert symbols[0]["signature"] == 'API_TOKEN = "[REDACTED_SECRET]"'


def test_cli_lookup_combines_symbols_and_text(tmp_path: Path, capsys) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)

    assert main(["lookup", "--repo", str(repo), "Service", "--limit", "8"]) == 0

    output = capsys.readouterr().out
    assert "src/app/service.py:1 symbol class Service" in output
    assert "src/app/service.py:1 file src/app/service.py" in output
    assert "src/app/api.py:1 text from .service import Service" in output


def test_lookup_keeps_exact_symbol_results_focused(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    for index in range(10):
        (repo / f"src/app/consumer_{index}.py").write_text(
            f"from .service import Service\n\ndef handler_{index}() -> str:\n    return Service().run()\n"
        )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = lookup(repo, store, "Service", limit=20)
    text_hits = [hit for hit in result.hits if hit.kind == "text"]

    assert result.text_matches <= 3
    assert len(text_hits) <= 3
    assert result.hits[0].label == "class Service"


def test_lookup_can_skip_text_search_for_known_symbols(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = lookup(repo, store, "Service", limit=20, text_limit=0)

    assert result.text_matches == 0
    assert {hit.kind for hit in result.hits} == {"symbol", "file"}
    assert result.hits[0].label == "class Service"


def test_lookup_zero_symbol_and_file_limits_skip_those_phases(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = lookup(repo, store, "Service", symbol_limit=0, file_limit=0, text_limit=2)

    assert result.symbols == 0
    assert result.files == 0
    assert result.text_matches > 0
    assert {hit.kind for hit in result.hits} == {"text"}


def test_lookup_source_first_excludes_tests_and_backfills_source_hits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "zzz").mkdir()
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    (repo / "zzz/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "zzz/usage.py").write_text("print(VALUE_CONFIGS)\n")
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    with_tests = lookup(repo, store, "VALUE_CONFIGS", limit=1, text_limit=0)
    source_first = lookup(repo, store, "VALUE_CONFIGS", limit=1, text_limit=0, include_tests=False)

    assert with_tests.hits[0].path == "tests/test_constants.py"
    assert source_first.text_matches == 0
    assert source_first.hits[0].path == "zzz/constants.py"
    assert all("tests/" not in hit.path for hit in source_first.hits)


def test_lookup_source_first_can_fallback_to_text_when_no_source_hits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    strict = lookup(repo, store, "Hardware Capacity Planner", limit=5, text_limit=0, include_tests=False)
    fallback = lookup(
        repo,
        store,
        "Hardware Capacity Planner",
        limit=5,
        text_limit=0,
        fallback_text_limit=5,
        include_tests=False,
    )

    assert strict.hits == []
    assert strict.text_matches == 0
    assert fallback.text_matches == 1
    assert fallback.hits[0].kind == "text"
    assert fallback.hits[0].path == "config/navigation.js"


def test_lookup_skips_fuzzy_symbol_scan_for_natural_ui_labels(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    def fail_fuzzy_symbol_scan(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("natural UI labels should not run broad fuzzy symbol search")

    monkeypatch.setattr("code_intel.lookup.search_symbols", fail_fuzzy_symbol_scan)

    fallback = lookup(
        repo,
        store,
        "Hardware Capacity Planner",
        limit=5,
        text_limit=0,
        fallback_text_limit=5,
        include_tests=False,
    )

    assert fallback.hits[0].kind == "text"
    assert fallback.hits[0].path == "config/navigation.js"


def test_workspace_lookup_source_first_skips_fuzzy_symbol_scan_for_missing_repo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backend = _make_python_repo(tmp_path)
    ui = tmp_path / "ui"
    (ui / "components").mkdir(parents=True)
    (ui / "components/EmptyPanel.jsx").write_text("export function EmptyPanel() {\n  return null;\n}\n")
    build_catalog(backend)
    build_catalog(ui)

    def fail_fuzzy_symbol_scan(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("source-first workspace lookup should not fuzzy scan repos with no prefix hit")

    monkeypatch.setattr("code_intel.lookup.search_symbols", fail_fuzzy_symbol_scan)

    result = workspace_lookup(
        [backend, ui],
        "Service",
        limit=5,
        text_limit=0,
        include_tests=False,
    )

    assert result.hits[0].kind == "symbol"
    assert result.hits[0].path == "src/app/service.py"


def test_workspace_lookup_text_fallback_does_not_repeat_symbol_file_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backend = _make_python_repo(tmp_path)
    ui = tmp_path / "ui"
    (ui / "config").mkdir(parents=True)
    (ui / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    build_catalog(backend)
    build_catalog(ui)
    search_counts = {"symbols": 0, "files": 0}
    original_symbol_search = CatalogStore.search_symbol_prefixes
    original_file_search = CatalogStore.search_files_many

    def count_symbol_search(self: CatalogStore, *args: Any, **kwargs: Any) -> Any:
        search_counts["symbols"] += 1
        return original_symbol_search(self, *args, **kwargs)

    def count_file_search(self: CatalogStore, *args: Any, **kwargs: Any) -> Any:
        search_counts["files"] += 1
        return original_file_search(self, *args, **kwargs)

    monkeypatch.setattr(CatalogStore, "search_symbol_prefixes", count_symbol_search)
    monkeypatch.setattr(CatalogStore, "search_files_many", count_file_search)

    result = workspace_lookup(
        [backend, ui],
        "Hardware Capacity Planner",
        limit=5,
        text_limit=0,
        fallback_text_limit=5,
        include_tests=False,
    )

    assert result.hits[0].kind == "text"
    assert result.hits[0].path == "config/navigation.js"
    assert search_counts == {"symbols": 2, "files": 2}


def test_lookup_source_first_uses_identifier_variants_for_natural_ui_labels(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "components/incoming-hardware").mkdir(parents=True)
    (repo / "components/incoming-hardware/IncomingHardwareActionHistoryPanel.jsx").write_text(
        "export function IncomingHardwareActionHistoryPanel() {\n"
        "  return <section>Incoming hardware operator action history</section>;\n"
        "}\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = lookup(repo, store, "Incoming Hardware", limit=5, text_limit=0, include_tests=False)

    assert result.text_matches == 0
    assert result.hits[0].kind == "symbol"
    assert result.hits[0].path == "components/incoming-hardware/IncomingHardwareActionHistoryPanel.jsx"
    assert result.hits[0].label == "component IncomingHardwareActionHistoryPanel"
    assert {hit.kind for hit in result.hits} <= {"symbol", "file"}


def test_workspace_lookup_can_use_supplied_catalog_stores(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo, reuse_connection=True)

    result = workspace_lookup([repo], "Service", stores_by_repo={str(repo.resolve()): store})

    assert result.repos[0].searched is True
    assert result.hits[0].path == "src/app/service.py"
    store.close()


def test_lookup_ranks_ui_label_entry_points_before_deep_rendered_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "components/reports/datacenter-racks").mkdir(parents=True)
    (repo / "config").mkdir(parents=True)
    (repo / "pages").mkdir(parents=True)
    (repo / "components/reports/datacenter-racks/DatacenterRackDashboard.jsx").write_text(
        "export function DatacenterRackDashboard() {\n"
        '  return <div className="text-sm font-semibold">Hardware Capacity Planner</div>;\n'
        "}\n"
    )
    (repo / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    (repo / "pages/DashboardPage.jsx").write_text(
        "export const cards = [\n  { title: 'Hardware Capacity Planner', to: '/dashboard/datacenter-racks' },\n];\n"
    )
    (repo / "pages/DatacenterRackDashboardPage.jsx").write_text(
        "export default function DatacenterRackDashboardPage() {\n"
        '  return <Layout title="Hardware Capacity Planner" />;\n'
        "}\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = lookup(
        repo,
        store,
        "Hardware Capacity Planner",
        limit=10,
        text_limit=0,
        fallback_text_limit=10,
        include_tests=False,
    )
    paths = [hit.path for hit in result.hits]

    assert paths[0] == "config/navigation.js"
    assert paths.index("components/reports/datacenter-racks/DatacenterRackDashboard.jsx") > paths.index(
        "pages/DatacenterRackDashboardPage.jsx"
    )


def test_lookup_prefers_source_definition_over_fuzzy_test_symbol(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/constants.py").write_text(
        "VALUE_CONFIGS: dict[str, str] = {\n"
        "    'alpha': 'beta',\n"
        "}\n"
        "\n"
        "def get_value(key: str) -> str:\n"
        "    return VALUE_CONFIGS[key]\n"
    )
    (repo / "tests/test_constants.py").write_text(
        "from src.app.constants import VALUE_CONFIGS\n\n"
        "class TestVALUE_CONFIGS:\n"
        "    def test_values(self) -> None:\n"
        "        assert VALUE_CONFIGS['alpha'] == 'beta'\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    result = lookup(repo, store, "VALUE_CONFIGS", limit=5)

    assert result.hits[0].kind == "symbol"
    assert result.hits[0].path == "src/app/constants.py"
    assert result.hits[0].line == 1
    assert result.hits[0].label == "constant VALUE_CONFIGS"


def test_cli_workspace_lookup_searches_multiple_catalogs(tmp_path: Path, capsys) -> None:
    backend = _make_python_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "workspace-lookup",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "SurfacePanel",
                "--limit",
                "5",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 1
    assert {repo["label"] for repo in payload["repos"]} == {"repo", "ui"}
    assert payload["hits"][0]["repo_label"] == "ui"
    assert payload["hits"][0]["path"] == "components/SurfacePanel.jsx"
    assert payload["hits"][0]["label"] == "component SurfacePanel"

    assert (
        main(
            [
                "workspace-lookup",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "SurfacePanel",
                "--limit",
                "5",
                "--text-limit",
                "0",
                "--json",
            ]
        )
        == 0
    )
    textless_payload = json.loads(capsys.readouterr().out)
    assert {hit["kind"] for hit in textless_payload["hits"]} <= {"symbol", "file"}


def test_workspace_lookup_preserves_repo_status_order_with_missing_catalog(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    backend = _make_python_repo(tmp_path)
    build_catalog(backend)

    result = workspace_lookup([missing, backend], "Service", limit=5)

    assert [Path(repo.repo_path).name for repo in result.repos] == ["missing", "repo"]
    assert result.repos[0].searched is False
    assert result.repos[0].error.startswith("No catalog found")
    assert result.repos[1].searched is True
    assert result.hits[0].repo_path == str(backend.resolve())
    assert result.hits[0].path == "src/app/service.py"


def test_cli_workspace_lookup_source_first_excludes_tests_and_text(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "zzz").mkdir()
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    (repo / "zzz/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "zzz/usage.py").write_text("print(VALUE_CONFIGS)\n")
    build_catalog(repo)

    assert (
        main(
            [
                "workspace-lookup",
                "--repo",
                str(repo),
                "VALUE_CONFIGS",
                "--limit",
                "1",
                "--source-first",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["hits"][0]["path"] == "zzz/constants.py"
    assert payload["hits"][0]["kind"] == "symbol"


def test_cli_workspace_lookup_source_first_falls_back_to_text_only_when_needed(
    tmp_path: Path,
    capsys,
) -> None:
    ui = tmp_path / "ui"
    (ui / "config").mkdir(parents=True)
    (ui / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    build_catalog(ui)

    assert (
        main(
            [
                "workspace-lookup",
                "--repo",
                str(ui),
                "Hardware Capacity Planner",
                "--source-first",
                "--json",
            ]
        )
        == 0
    )
    fallback_payload = json.loads(capsys.readouterr().out)

    assert fallback_payload["count"] == 1
    assert fallback_payload["hits"][0]["kind"] == "text"
    assert fallback_payload["hits"][0]["path"] == "config/navigation.js"

    assert (
        main(
            [
                "workspace-lookup",
                "--repo",
                str(ui),
                "Hardware Capacity Planner",
                "--source-first",
                "--text-limit",
                "0",
                "--json",
            ]
        )
        == 0
    )
    strict_payload = json.loads(capsys.readouterr().out)
    assert strict_payload["count"] == 0


def test_cli_workspace_lookup_finds_file_stems_across_catalogs(tmp_path: Path, capsys) -> None:
    backend = _make_python_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    assert (
        main(
            [
                "workspace-lookup",
                "--repo",
                str(backend),
                "--repo",
                str(ui),
                "rackCapacity",
                "--limit",
                "5",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"][0]["repo_label"] == "ui"
    assert payload["hits"][0]["kind"] == "file"
    assert payload["hits"][0]["path"] == "utils/rackCapacity.js"


def test_cli_workspace_lookup_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_python_repo(tmp_path)
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
                "--json",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["name"] == "void"

    assert main(["workspace-list", "--workspace-dir", str(workspace_dir), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [workspace["name"] for workspace in listed] == ["void"]

    assert (
        main(
            [
                "workspace-lookup",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "SurfacePanel",
                "--limit",
                "5",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"][0]["repo_label"] == "ui"
    assert payload["hits"][0]["path"] == "components/SurfacePanel.jsx"


def test_incremental_scan_reuses_unchanged_files(tmp_path: Path, capsys) -> None:
    repo = _make_python_repo(tmp_path)

    full = build_catalog(repo, workers=1)
    incremental = build_catalog(repo, incremental=True, workers=1)

    assert full.incremental is False
    assert incremental.incremental is True
    assert incremental.reused_file_count == 4
    assert incremental.changed_file_count == 0
    assert incremental.written_file_count == 0
    assert incremental.file_count == 4
    assert main(["scan", str(repo), "--incremental", "--workers", "1"]) == 0
    assert "incremental: reused=4, changed=0, removed=0" in capsys.readouterr().out


def test_parallel_scan_matches_serial_catalog(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    serial_db = tmp_path / "serial.sqlite"
    parallel_db = tmp_path / "parallel.sqlite"

    serial = build_catalog(repo, serial_db, workers=1)
    parallel = build_catalog(repo, parallel_db, workers=4)
    serial_store = CatalogStore(serial_db)
    parallel_store = CatalogStore(parallel_db)

    assert serial.file_count == parallel.file_count
    assert serial.symbol_count == parallel.symbol_count
    assert serial.dependency_count == parallel.dependency_count
    assert serial_store.get_meta()["analysis_workers"] == "1"
    assert parallel_store.get_meta()["analysis_workers"] == "4"
    assert [dict(row) for row in serial_store.search_symbols("Service")] == [
        dict(row) for row in parallel_store.search_symbols("Service")
    ]


def test_default_scan_uses_serial_workers_for_small_repos(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)

    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    assert store.get_meta()["analysis_workers"] == "1"


def test_incremental_scan_fast_path_skips_analysis_loads(tmp_path: Path, monkeypatch) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)

    def fail_load_file_analysis(self: CatalogStore, path: str):
        raise AssertionError(f"unexpected analysis load for {path}")

    monkeypatch.setattr(CatalogStore, "load_file_analysis", fail_load_file_analysis)

    incremental = build_catalog(repo, incremental=True)

    assert incremental.reused_file_count == 4
    assert incremental.changed_file_count == 0


def test_incremental_scan_can_skip_discovery_when_git_status_fingerprint_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _make_git_repo_with_untracked_source(tmp_path)
    build_catalog(repo)

    def fail_discover_source_file_entries(_repo_root: Path, _supported_extensions: frozenset[str]):
        raise AssertionError("matching Git status fingerprint should skip discovery")

    monkeypatch.setattr(cataloger, "discover_source_file_entries", fail_discover_source_file_entries)

    incremental = build_catalog(repo, incremental=True, skip_unchanged_meta=True)

    assert incremental.reused_file_count == 2
    assert incremental.changed_file_count == 0
    assert incremental.written_file_count == 0
    assert incremental.timings_ms["discovery"] == 0.0
    assert incremental.timings_ms["fast_path"] == 1.0


def test_incremental_scan_discovers_when_git_status_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    repo = _make_git_repo_with_untracked_source(tmp_path)
    build_catalog(repo)
    (repo / "src/extra.py").write_text("EXTRA_VALUE = 'changed and longer'\n")
    called = False
    original_discover = cataloger.discover_source_file_entries

    def track_discover_source_file_entries(repo_root: Path, supported_extensions: frozenset[str]):
        nonlocal called
        called = True
        return original_discover(repo_root, supported_extensions)

    monkeypatch.setattr(cataloger, "discover_source_file_entries", track_discover_source_file_entries)

    incremental = build_catalog(repo, incremental=True, skip_unchanged_meta=True)

    assert called
    assert incremental.changed_file_count == 1
    assert incremental.written_file_count == 1
    assert "fast_path" not in incremental.timings_ms


def test_incremental_scan_discovers_when_ancestor_gitignore_changes(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    ui_src = repo / "ui/src"
    ui_src.mkdir(parents=True)
    (repo / ".gitignore").write_text("")
    (ui_src / "App.jsx").write_text("export function App() { return null; }\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", ".gitignore", "ui/src/App.jsx"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    build_catalog(ui_src)
    (repo / ".gitignore").write_text("ignored.js\n")
    called = False
    original_discover = cataloger.discover_source_file_entries

    def track_discover_source_file_entries(repo_root: Path, supported_extensions: frozenset[str]):
        nonlocal called
        called = True
        return original_discover(repo_root, supported_extensions)

    monkeypatch.setattr(cataloger, "discover_source_file_entries", track_discover_source_file_entries)

    incremental = build_catalog(ui_src, incremental=True, skip_unchanged_meta=True)

    assert called
    assert incremental.changed_file_count == 0
    assert "fast_path" not in incremental.timings_ms


def test_incremental_stat_match_uses_discovered_stats_without_restating(tmp_path: Path) -> None:
    entry = DiscoveredSourceFile(
        path=tmp_path / "missing.py",
        rel_path="src/app.py",
        size_bytes=10,
        modified_ns=20,
    )

    assert cataloger._all_file_stats_match([entry], {"src/app.py": {"size_bytes": "10", "modified_ns": "20"}})


def test_incremental_scan_rebuilds_when_analyzer_version_changes(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)
    store.update_meta({"analyzer_version": "old"})

    incremental = build_catalog(repo, incremental=True)

    assert incremental.incremental is False
    assert incremental.reused_file_count == 0
    assert incremental.changed_file_count == 4
    assert incremental.written_file_count == 4
    assert store.get_meta()["analyzer_version"] == CATALOG_ANALYZER_VERSION


def test_incremental_scan_can_skip_no_change_metadata_write(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)
    store.update_meta({"generated_at": "sentinel"})

    skipped = build_catalog(repo, incremental=True, skip_unchanged_meta=True)

    assert skipped.reused_file_count == 4
    assert skipped.changed_file_count == 0
    assert skipped.written_file_count == 0
    assert skipped.timings_ms["write"] < 1.0
    assert store.get_meta()["generated_at"] == "sentinel"

    updated = build_catalog(repo, incremental=True)

    assert updated.changed_file_count == 0
    assert store.get_meta()["generated_at"] != "sentinel"


def test_incremental_scan_reparses_changed_file(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    service_path = repo / "src/app/service.py"
    service_path.write_text(
        "class Service:\n"
        "    def run(self) -> str:\n"
        "        return 'changed'\n"
        "\n"
        "def new_helper() -> str:\n"
        "    return Service().run()\n"
    )

    incremental = build_catalog(repo, incremental=True)
    store = CatalogStore.for_repo(repo)
    symbols = store.symbols_for_file("src/app/service.py")

    assert incremental.incremental is True
    assert incremental.reused_file_count == 3
    assert incremental.changed_file_count == 1
    assert incremental.written_file_count == 1
    assert "new_helper" in {str(row["name"]) for row in symbols}


def test_reusable_store_caches_file_symbol_and_line_rows(tmp_path: Path) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo, reuse_connection=True)
    statements: list[str] = []
    store.connect().set_trace_callback(statements.append)

    assert store.get_file("src/app/service.py") is not None
    assert store.get_file("src/app/service.py") is not None
    assert store.symbols_for_file("src/app/service.py")
    assert store.symbols_for_file("src/app/service.py")
    assert store.source_line_range("src/app/service.py", 1, 3)
    assert store.source_line_range("src/app/service.py", 1, 3)
    assert store.file_token_summary(selected_paths={"src/app/service.py"})["selected_tokens"] > 0
    assert store.file_token_summary(selected_paths={"src/app/service.py"})["selected_tokens"] > 0

    assert _statement_count(statements, "SELECT * FROM files WHERE path =") == 1
    assert _statement_count(statements, "SELECT * FROM symbols WHERE path =") == 1
    assert _statement_count(statements, "FROM text_lines") == 1
    assert _statement_count(statements, "SELECT COUNT(*) AS file_count") == 1
    assert _statement_count(statements, "WHERE path IN") == 1


def test_incremental_scan_updates_only_changed_text_index_rows(tmp_path: Path, monkeypatch) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)
    assert store.search_text("return 'ok'", limit=5, context_lines=0)

    def fail_rebuild_text_index(_self: CatalogStore, _connection) -> None:
        raise AssertionError("incremental scan should not rebuild the full text index")

    monkeypatch.setattr(CatalogStore, "_rebuild_text_index", fail_rebuild_text_index)
    (repo / "src/app/service.py").write_text("class Service:\n    def run(self) -> str:\n        return 'changed'\n")

    incremental = build_catalog(repo, incremental=True)

    assert incremental.reused_file_count == 3
    assert incremental.changed_file_count == 1
    assert incremental.written_file_count == 1
    assert store.search_text("return 'changed'", limit=5, context_lines=0)[0].path == "src/app/service.py"
    assert store.search_text("return 'ok'", limit=5, context_lines=0) == []


def test_incremental_scan_adds_new_file_without_full_rebuild(tmp_path: Path, monkeypatch) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    analyzed_paths: list[str] = []
    real_analyze_file = cataloger._analyze_file

    def track_analyze_file(path: Path, repo_root: Path, all_paths: set[str], fingerprint: cataloger.FileFingerprint):
        analyzed_paths.append(path.relative_to(repo_root).as_posix())
        return real_analyze_file(path, repo_root, all_paths, fingerprint)

    monkeypatch.setattr(cataloger, "_analyze_file", track_analyze_file)
    (repo / "src/app/new_feature.py").write_text("def added() -> str:\n    return 'added'\n")

    incremental = build_catalog(repo, incremental=True)
    store = CatalogStore.for_repo(repo)

    assert incremental.incremental is True
    assert incremental.reused_file_count == 4
    assert incremental.changed_file_count == 1
    assert incremental.removed_file_count == 0
    assert incremental.written_file_count == 1
    assert analyzed_paths == ["src/app/new_feature.py"]
    assert store.resolve_file_path("new_feature.py") == "src/app/new_feature.py"
    assert "added" in {str(row["name"]) for row in store.symbols_for_file("src/app/new_feature.py")}


def test_incremental_scan_removes_file_and_reanalyzes_dependents(tmp_path: Path, monkeypatch) -> None:
    repo = _make_python_repo(tmp_path)
    build_catalog(repo)
    analyzed_paths: list[str] = []
    real_analyze_file = cataloger._analyze_file

    def track_analyze_file(path: Path, repo_root: Path, all_paths: set[str], fingerprint: cataloger.FileFingerprint):
        analyzed_paths.append(path.relative_to(repo_root).as_posix())
        return real_analyze_file(path, repo_root, all_paths, fingerprint)

    monkeypatch.setattr(cataloger, "_analyze_file", track_analyze_file)
    (repo / "src/app/service.py").unlink()

    incremental = build_catalog(repo, incremental=True)
    store = CatalogStore.for_repo(repo)

    assert incremental.incremental is True
    assert incremental.reused_file_count == 1
    assert incremental.changed_file_count == 2
    assert incremental.removed_file_count == 1
    assert incremental.written_file_count == 2
    assert sorted(analyzed_paths) == ["src/app/api.py", "tests/test_service.py"]
    assert store.resolve_file_path("src/app/service.py") is None
    assert store.dependents_for_file("src/app/service.py") == []


def test_incremental_scan_added_file_resolves_existing_unresolved_import(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "src/app/__init__.py").write_text("")
    (repo / "src/app/api.py").write_text(
        "from .new_service import NewService\n\ndef handler() -> NewService:\n    return NewService()\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)
    initial_dependency = store.dependencies_for_file("src/app/api.py")[0]
    assert bool(initial_dependency["resolved"]) is False

    (repo / "src/app/new_service.py").write_text("class NewService:\n    pass\n")

    incremental = build_catalog(repo, incremental=True)
    updated_dependency = store.dependencies_for_file("src/app/api.py")[0]

    assert incremental.incremental is True
    assert incremental.reused_file_count == 1
    assert incremental.changed_file_count == 2
    assert incremental.written_file_count == 2
    assert bool(updated_dependency["resolved"]) is True
    assert str(updated_dependency["category"]) == "code"
    assert str(updated_dependency["target_path"]) == "src/app/new_service.py"


def _make_python_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/__init__.py").write_text("")
    (repo / "src/app/service.py").write_text("class Service:\n    def run(self) -> str:\n        return 'ok'\n")
    (repo / "src/app/api.py").write_text(
        "from .service import Service\n\ndef handler() -> str:\n    return Service().run()\n"
    )
    (repo / "tests/test_service.py").write_text(
        "from src.app.service import Service\n\ndef test_service() -> None:\n    assert Service().run() == 'ok'\n"
    )
    return repo


def _make_git_repo_with_untracked_source(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("def app() -> str:\n    return 'ok'\n")
    (repo / "src/extra.py").write_text("EXTRA_VALUE = 'dirty'\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "src/app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _statement_count(statements: list[str], pattern: str) -> int:
    return sum(1 for statement in statements if pattern in statement)


def _make_ui_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ui"
    (repo / "components").mkdir(parents=True)
    (repo / "utils").mkdir()
    (repo / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    (repo / "utils/rackCapacity.js").write_text("const DEFAULT_RACK_U = 48;\n")
    return repo
