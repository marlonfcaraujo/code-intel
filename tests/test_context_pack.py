from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.cli import main
from code_intel.context_pack import build_context_pack, build_workspace_context_pack, context_pack_to_dict
from code_intel.workspace_config import save_workspace_config


def test_build_context_pack_returns_bounded_symbol_context(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src/app/noise.py").write_text("\n".join(f"NOISE_{index} = {index}" for index in range(200)))
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    pack = build_context_pack(repo, store, "Service", max_files=1, context_lines=1, max_lines_per_file=4)

    assert pack.query == "Service"
    assert pack.candidate_files == 2
    assert pack.selected_files == 1
    assert pack.selected_lines <= 4
    assert pack.snippets[0].path == "src/app/service.py"
    assert "class Service:" in pack.snippets[0].content
    assert pack.estimated_tokens > 0
    assert pack.estimated_saved_tokens > 0


def test_build_workspace_context_pack_returns_ui_context(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    pack = build_workspace_context_pack([backend, ui], "SurfacePanel", max_files=1, max_lines_per_file=20)

    assert pack.repo_paths == [str(backend.resolve()), str(ui.resolve())]
    assert pack.candidate_files == 2
    assert pack.snippets[0].repo_label == "ui"
    assert pack.snippets[0].path == "components/SurfacePanel.jsx"
    assert "export function SurfacePanel" in pack.snippets[0].content


def test_context_pack_to_dict_can_serialize_compact_repo_indexes(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    build_catalog(backend)
    build_catalog(ui)

    pack = build_workspace_context_pack([backend, ui], "SurfacePanel", max_files=1, max_lines_per_file=20)
    full = context_pack_to_dict(pack)
    compact = context_pack_to_dict(pack, compact=True)
    compact_without_hits = context_pack_to_dict(pack, compact=True, include_hits=False)

    assert full["hits"][0]["repo_path"] == str(ui.resolve())
    assert "score" in full["hits"][0]
    assert "repo_index" not in full["hits"][0]
    assert full["snippets"][0]["repo_path"] == str(ui.resolve())
    assert "line_count" in full["snippets"][0]
    assert "repo_index" not in full["snippets"][0]
    assert compact["repo_paths"] == [str(backend.resolve()), str(ui.resolve())]
    assert compact["repo_labels"] == ["repo", "ui"]
    assert compact["hits"][0]["repo_index"] == 1
    assert compact["snippets"][0]["repo_index"] == 1
    assert "repo_path" not in compact["hits"][0]
    assert "repo_path" not in compact["snippets"][0]
    assert "repo_label" not in compact["hits"][0]
    assert "repo_label" not in compact["snippets"][0]
    assert "score" not in compact["hits"][0]
    assert "line_count" not in compact["snippets"][0]
    assert compact["snippets"][0]["content"] == full["snippets"][0]["content"]
    assert "hits" not in compact_without_hits
    assert compact_without_hits["hit_count"] == compact["hit_count"]
    assert compact_without_hits["snippets"][0]["content"] == full["snippets"][0]["content"]


def test_context_pack_file_hit_samples_symbols_across_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    (repo / "utils/rackCapacity.js").write_text(
        "export const DEFAULT_RACK_CAPACITY = 42;\n"
        "\n"
        "function internalHelper(value) {\n"
        "  return value;\n"
        "}\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "export function normalizeRackCapacity(value) {\n"
        "  return internalHelper(value);\n"
        "}\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "export function applyRackCapacityPolicy(rack) {\n"
        "  return rack;\n"
        "}\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "export function summarizeRackCapacity(racks) {\n"
        "  return racks.length;\n"
        "}\n"
    )
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    pack = build_context_pack(repo, store, "rackCapacity", max_files=1, context_lines=1, max_lines_per_file=12)
    content = "\n".join(snippet.content for snippet in pack.snippets)

    assert pack.selected_files == 1
    assert pack.selected_lines <= 12
    assert "export const DEFAULT_RACK_CAPACITY" in content
    assert "export function applyRackCapacityPolicy" in content
    assert "export function summarizeRackCapacity" in content
    assert "function internalHelper(value)" not in content


def test_context_pack_uses_catalog_lines_for_unchanged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "components").mkdir(parents=True)
    panel_file = repo / "components/LargePanel.jsx"
    panel_file.write_text("export function LargePanel() {\n  return <section>Large panel</section>;\n}\n")
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    original_read_text = Path.read_text

    def fail_panel_file_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == panel_file:
            raise AssertionError("unchanged files should use catalog-backed line ranges")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_panel_file_read)

    pack = build_context_pack(repo, store, "LargePanel", max_files=1, context_lines=1, max_lines_per_file=5)

    assert pack.snippets[0].path == "components/LargePanel.jsx"
    assert "export function LargePanel" in pack.snippets[0].content


def test_context_pack_reads_disk_when_cataloged_file_changed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "components").mkdir(parents=True)
    panel_file = repo / "components/LargePanel.jsx"
    panel_file.write_text("export function LargePanel() {\n  return <section>Large panel</section>;\n}\n")
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)
    panel_file.write_text(
        "export function LargePanel() {\n  return <section>Updated panel content</section>;\n}\nconst changed = true;\n"
    )

    pack = build_context_pack(repo, store, "LargePanel", max_files=1, context_lines=1, max_lines_per_file=5)

    assert "Updated panel content" in pack.snippets[0].content


def test_context_pack_can_exclude_test_file_snippets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    with_tests = build_context_pack(repo, store, "VALUE_CONFIGS", max_files=2, text_limit=0)
    source_only = build_context_pack(
        repo,
        store,
        "VALUE_CONFIGS",
        max_files=2,
        text_limit=0,
        include_tests=False,
    )

    assert {snippet.path for snippet in with_tests.snippets} == {
        "src/app/constants.py",
        "tests/test_constants.py",
    }
    assert {snippet.path for snippet in source_only.snippets} == {"src/app/constants.py"}


def test_context_pack_backfills_source_hits_when_excluding_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "zzz").mkdir()
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    (repo / "zzz/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    with_tests = build_context_pack(repo, store, "VALUE_CONFIGS", limit=1, max_files=1, text_limit=0)
    source_only = build_context_pack(
        repo,
        store,
        "VALUE_CONFIGS",
        limit=1,
        max_files=1,
        text_limit=0,
        include_tests=False,
    )

    assert with_tests.hits[0].path == "tests/test_constants.py"
    assert source_only.hits[0].path == "zzz/constants.py"
    assert source_only.snippets[0].path == "zzz/constants.py"


def test_cli_context_pack_outputs_json(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    assert main(["context-pack", "--repo", str(repo), "Service", "--max-files", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_files"] == 1
    assert payload["selected_files"] == 1
    assert "estimated_saved_tokens" in payload
    assert payload["snippets"][0]["path"] == "src/app/service.py"


def test_cli_context_pack_can_exclude_tests(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "src/app/usage.py").write_text("print(VALUE_CONFIGS)\n")
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    build_catalog(repo)

    assert (
        main(
            [
                "context-pack",
                "--repo",
                str(repo),
                "VALUE_CONFIGS",
                "--max-files",
                "2",
                "--source-first",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert {snippet["path"] for snippet in payload["snippets"]} == {"src/app/constants.py"}


def test_cli_workspace_context_uses_named_workspace(tmp_path: Path, capsys) -> None:
    backend = _make_repo(tmp_path)
    ui = _make_ui_repo(tmp_path)
    workspace_dir = tmp_path / "workspaces"
    build_catalog(backend)
    build_catalog(ui)
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    assert (
        main(
            [
                "workspace-context",
                "--workspace",
                "void",
                "--workspace-dir",
                str(workspace_dir),
                "SurfacePanel",
                "--max-files",
                "1",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_files"] == 2
    assert payload["selected_files"] == 1
    assert "estimated_saved_tokens" in payload
    assert payload["snippets"][0]["repo_label"] == "ui"
    assert payload["snippets"][0]["path"] == "components/SurfacePanel.jsx"


def test_workspace_context_source_first_falls_back_for_ui_labels(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = tmp_path / "ui"
    (ui / "components/reports/datacenter-racks").mkdir(parents=True)
    (ui / "config").mkdir(parents=True)
    (ui / "pages").mkdir(parents=True)
    (ui / "components/reports/datacenter-racks/DatacenterRackDashboard.jsx").write_text(
        "export function DatacenterRackDashboard() {\n"
        '  return <div className="text-sm font-semibold">Hardware Capacity Planner</div>;\n'
        "}\n"
    )
    (ui / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    (ui / "pages/DatacenterRackDashboardPage.jsx").write_text(
        "export default function DatacenterRackDashboardPage() {\n"
        '  return <Layout title="Hardware Capacity Planner" />;\n'
        "}\n"
    )
    build_catalog(backend)
    build_catalog(ui)

    pack = build_workspace_context_pack(
        [backend, ui],
        "Hardware Capacity Planner",
        max_files=1,
        text_limit=0,
        fallback_text_limit=5,
        include_tests=False,
    )

    assert pack.hit_count == 3
    assert pack.hits[0].path == "config/navigation.js"
    assert pack.snippets[0].repo_label == "ui"
    assert pack.snippets[0].path == "config/navigation.js"
    assert "Hardware Capacity Planner" in pack.snippets[0].content


def test_workspace_context_source_first_uses_identifier_variants_before_text_fallback(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = tmp_path / "ui"
    (ui / "components/incoming-hardware").mkdir(parents=True)
    (ui / "components/incoming-hardware/IncomingHardwareActionHistoryPanel.jsx").write_text(
        "export function IncomingHardwareActionHistoryPanel() {\n"
        "  return <section>Incoming hardware operator action history</section>;\n"
        "}\n"
    )
    build_catalog(backend)
    build_catalog(ui)

    pack = build_workspace_context_pack(
        [backend, ui],
        "Incoming Hardware",
        max_files=1,
        text_limit=0,
        fallback_text_limit=5,
        include_tests=False,
    )

    assert pack.hit_count >= 1
    assert pack.hits[0].kind == "symbol"
    assert pack.hits[0].path == "components/incoming-hardware/IncomingHardwareActionHistoryPanel.jsx"
    assert pack.snippets[0].repo_label == "ui"
    assert pack.snippets[0].path == "components/incoming-hardware/IncomingHardwareActionHistoryPanel.jsx"
    assert "export function IncomingHardwareActionHistoryPanel" in pack.snippets[0].content


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
    return repo


def _make_ui_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ui"
    (repo / "components").mkdir(parents=True)
    (repo / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    return repo
