from __future__ import annotations

from pathlib import Path

from code_intel import mcp_server
from code_intel.cataloger import build_catalog
from code_intel.mcp_server import (
    catalog_health_tool,
    catalog_repo_tool,
    context_pack_tool,
    explain_file_tool,
    find_references_tool,
    find_symbols_tool,
    get_file_content_tool,
    get_file_outline_tool,
    get_file_tree_tool,
    list_workspaces_tool,
    lookup_tool,
    related_tests_tool,
    repo_outline_tool,
    risk_report_tool,
    savings_report_tool,
    search_text_tool,
    workflow_benchmark_tool,
    workspace_context_many_tool,
    workspace_context_tool,
    workspace_lookup_tool,
    workspace_outline_tool,
    workspace_references_tool,
)
from code_intel.workspace_config import save_workspace_config


def test_mcp_tool_helpers_return_catalog_data(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    catalog = catalog_repo_tool(str(repo), workers=1)
    incremental_catalog = catalog_repo_tool(str(repo), incremental=True, workers=1)
    skipped_meta_catalog = catalog_repo_tool(str(repo), incremental=True, workers=1, skip_unchanged_meta=True)
    symbols = find_symbols_tool("Service", repo_path=str(repo))
    lookup = lookup_tool("Service", repo_path=str(repo), limit=5)
    explanation = explain_file_tool("src/app/service.py", repo_path=str(repo))
    tests = related_tests_tool("src/app/service.py", repo_path=str(repo))
    outline = get_file_outline_tool("src/app/service.py", repo_path=str(repo))
    tree = get_file_tree_tool(repo_path=str(repo), prefix="src", max_depth=2)
    repo_outline = repo_outline_tool(repo_path=str(repo), max_depth=2, top_files=3)
    content = get_file_content_tool("src/app/service.py", repo_path=str(repo), start_line=1, end_line=1)
    text_matches = search_text_tool("VALUE_42", repo_path=str(repo), context_lines=0)
    references = find_references_tool("Service", repo_path=str(repo), context_lines=0)
    reference_summary = find_references_tool("Service", repo_path=str(repo), context_lines=0, summary_only=True)
    context_pack = context_pack_tool("Service", repo_path=str(repo), max_files=1)
    context_pack_with_hits = context_pack_tool("Service", repo_path=str(repo), max_files=1, include_hits=True)
    risk = risk_report_tool(repo_path=str(repo), limit=5)
    health = catalog_health_tool(str(repo))
    health_summary = catalog_health_tool(str(repo), summary_only=True)
    savings = savings_report_tool(str(repo))

    assert catalog["file_count"] == 4
    assert catalog_health_tool(str(repo))["meta"]["analysis_workers"] == "1"
    assert incremental_catalog["incremental"] is True
    assert incremental_catalog["reused_file_count"] == 4
    assert skipped_meta_catalog["written_file_count"] == 0
    assert skipped_meta_catalog["timings_ms"]["write"] < 1.0
    assert symbols["symbols"][0]["path"] == "src/app/service.py"
    assert lookup["hits"][0]["kind"] == "symbol"
    assert {hit["kind"] for hit in lookup["hits"]} == {"symbol", "file", "text"}
    assert "src/app/api.py" in explanation["direct_dependents"]
    assert tests["tests"][0]["path"] == "tests/test_service.py"
    assert outline["symbols"][0]["name"] == "Service"
    assert any(entry["path"] == "src/app/service.py" for entry in tree["entries"])
    assert repo_outline["total_files"] == 4
    assert repo_outline["directories"]
    assert content["content"] == "class Service:"
    assert text_matches["matches"][0]["path"] == "src/app/extra.py"
    assert text_matches["matches"][0]["line"] == 43
    assert references["matches"][0]["kind"] == "definition"
    assert references["selected_files"] >= 2
    assert reference_summary["matches"] == []
    assert reference_summary["file_summaries"]
    assert context_pack["candidate_files"] == 4
    assert context_pack["estimated_saved_tokens"] > 0
    assert context_pack["repo_paths"] == [str(repo.resolve())]
    assert context_pack["repo_labels"] == ["repo"]
    assert "hits" not in context_pack
    assert context_pack_with_hits["hits"][0]["path"] == "src/app/service.py"
    assert context_pack["snippets"][0]["repo_index"] == 0
    assert "repo_path" not in context_pack["snippets"][0]
    assert "repo_label" not in context_pack["snippets"][0]
    assert context_pack["snippets"][0]["path"] == "src/app/service.py"
    assert risk["files"]
    assert health["catalog_exists"] is True
    assert health["freshness"]["status"] == "fresh"
    assert health["freshness"]["supports_text_index"] is True
    assert health["text_lines"] > 0
    assert health_summary["freshness"]["status"] == "fresh"
    assert "dependency_summary" not in health_summary
    assert "git_dirty_paths" not in health_summary["freshness"]
    assert savings["events"] >= 10
    assert savings["estimated_saved_tokens"] > 0


def test_mcp_read_tools_reuse_thread_catalog_store(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    mcp_server._invalidate_catalog_store(repo)
    original_for_repo = mcp_server.CatalogStore.for_repo
    calls: list[tuple[str, bool]] = []

    def counting_for_repo(
        repo_root: Path,
        database_path: Path | None = None,
        *,
        reuse_connection: bool = False,
    ) -> mcp_server.CatalogStore:
        calls.append((str(Path(repo_root).resolve()), reuse_connection))
        return original_for_repo(repo_root, database_path, reuse_connection=reuse_connection)

    monkeypatch.setattr(mcp_server.CatalogStore, "for_repo", staticmethod(counting_for_repo))

    first = context_pack_tool("Service", repo_path=str(repo), max_files=1)
    second = context_pack_tool("Service", repo_path=str(repo), max_files=1)
    workspace_lookup = workspace_lookup_tool("Service", repos=[str(repo)], limit=5)
    workspace_references = workspace_references_tool("Service", repos=[str(repo)], limit=5)
    workspace_outline = workspace_outline_tool(repos=[str(repo)], top_files=2)

    assert first["snippets"][0]["path"] == "src/app/service.py"
    assert second["snippets"][0]["path"] == "src/app/service.py"
    assert workspace_lookup["hits"][0]["path"] == "src/app/service.py"
    assert workspace_references["matches"][0]["path"] == "src/app/service.py"
    assert workspace_outline["repos"][0]["searched"] is True
    assert calls == [(str(repo.resolve()), True)]
    mcp_server._invalidate_catalog_store(repo)


def test_mcp_related_tests_handles_unknown_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    result = related_tests_tool("missing.py", repo_path=str(repo))

    assert result["ok"] is False
    assert result["tests"] == []


def test_mcp_workspace_lookup_searches_multiple_catalogs(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = tmp_path / "ui"
    (ui / "components").mkdir(parents=True)
    (ui / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    build_catalog(backend)
    build_catalog(ui)

    result = workspace_lookup_tool("SurfacePanel", repos=[str(backend), str(ui)], limit=5)

    assert result["count"] >= 1
    assert result["hits"][0]["repo_label"] == "ui"
    assert result["hits"][0]["label"] == "component SurfacePanel"


def test_mcp_lookup_source_first_excludes_tests_and_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "zzz").mkdir()
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    (repo / "zzz/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "zzz/usage.py").write_text("print(VALUE_CONFIGS)\n")
    build_catalog(repo)

    lookup = lookup_tool("VALUE_CONFIGS", repo_path=str(repo), limit=1, source_first=True)
    workspace_lookup = workspace_lookup_tool("VALUE_CONFIGS", repos=[str(repo)], limit=1, source_first=True)

    assert lookup["text_matches"] == 0
    assert lookup["hits"][0]["path"] == "zzz/constants.py"
    assert workspace_lookup["hits"][0]["path"] == "zzz/constants.py"
    assert workspace_lookup["hits"][0]["kind"] == "symbol"


def test_mcp_source_first_falls_back_to_text_for_ui_labels(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    (ui / "config").mkdir(parents=True)
    (ui / "config/navigation.js").write_text(
        "export const routes = [\n  { to: '/dashboard/datacenter-racks', label: 'Hardware Capacity Planner' },\n];\n"
    )
    build_catalog(ui)

    lookup = workspace_lookup_tool("Hardware Capacity Planner", repos=[str(ui)], source_first=True)
    strict_lookup = workspace_lookup_tool(
        "Hardware Capacity Planner",
        repos=[str(ui)],
        source_first=True,
        text_limit=0,
    )
    workflow = workflow_benchmark_tool(
        ["Hardware Capacity Planner"],
        repos=[str(ui)],
        repeat=1,
        warmup=0,
        max_files=1,
        source_first=True,
        summary_only=False,
    )

    assert lookup["hits"][0]["kind"] == "text"
    assert lookup["hits"][0]["path"] == "config/navigation.js"
    assert strict_lookup["count"] == 0
    assert workflow["queries"][0]["selected_paths"] == ["config/navigation.js"]
    assert workflow["queries"][0]["top_hits"][0]["kind"] == "text"


def test_mcp_workspace_lookup_uses_named_workspace(tmp_path: Path) -> None:
    backend = _make_repo(tmp_path)
    ui = tmp_path / "ui"
    workspace_dir = tmp_path / "workspaces"
    (ui / "components").mkdir(parents=True)
    (ui / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    build_catalog(backend)
    build_catalog(ui)
    save_workspace_config("void", [backend, ui], config_dir=workspace_dir)

    workspaces = list_workspaces_tool(workspace_dir=str(workspace_dir))
    result = workspace_lookup_tool("SurfacePanel", workspace_name="void", workspace_dir=str(workspace_dir), limit=5)
    outline = workspace_outline_tool(workspace_name="void", workspace_dir=str(workspace_dir), top_files=2)
    references = workspace_references_tool(
        "SurfacePanel",
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
    )
    reference_summary = workspace_references_tool(
        "SurfacePanel",
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
        summary_only=True,
    )
    context = workspace_context_tool(
        "SurfacePanel",
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
        max_files=1,
    )
    context_with_hits = workspace_context_tool(
        "SurfacePanel",
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
        max_files=1,
        include_hits=True,
    )
    contexts = workspace_context_many_tool(
        ["SurfacePanel", "Service"],
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
        max_files=1,
    )
    contexts_with_hits = workspace_context_many_tool(
        ["SurfacePanel"],
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
        max_files=1,
        include_hits=True,
    )
    duplicate_contexts = workspace_context_many_tool(
        ["SurfacePanel", "SurfacePanel", "SurfacePanel"],
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        limit=5,
        max_files=1,
    )
    workflow = workflow_benchmark_tool(
        ["SurfacePanel"],
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        repeat=1,
        warmup=0,
        max_files=1,
    )
    workflow_full = workflow_benchmark_tool(
        ["SurfacePanel"],
        workspace_name="void",
        workspace_dir=str(workspace_dir),
        repeat=1,
        warmup=0,
        max_files=1,
        summary_only=False,
    )

    assert workspaces["workspaces"][0]["name"] == "void"
    assert result["count"] >= 1
    assert result["hits"][0]["repo_label"] == "ui"
    assert result["hits"][0]["path"] == "components/SurfacePanel.jsx"
    assert outline["repo_count"] == 2
    assert outline["searched_repos"] == 2
    assert any(repo["label"] == "ui" for repo in outline["repos"])
    assert references["matches"][0]["repo_label"] == "ui"
    assert references["matches"][0]["kind"] == "definition"
    assert reference_summary["matches"] == []
    assert reference_summary["file_summaries"][0]["repo_label"] == "ui"
    assert context["candidate_files"] == 5
    assert context["repo_labels"] == ["repo", "ui"]
    assert "hits" not in context
    assert context_with_hits["hits"][0]["repo_index"] == 1
    assert context["snippets"][0]["repo_index"] == 1
    assert "repo_path" not in context["snippets"][0]
    assert "repo_label" not in context["snippets"][0]
    assert context["snippets"][0]["path"] == "components/SurfacePanel.jsx"
    assert contexts["mode"] == "workspace-context-many"
    assert contexts["query_count"] == 2
    assert contexts["repo_labels"] == ["repo", "ui"]
    assert contexts["total_selected_files"] == 2
    assert "shared_snippets" not in contexts
    assert contexts["contexts"][0]["query"] == "SurfacePanel"
    assert contexts["contexts"][0]["snippets"][0]["repo_index"] == 1
    assert contexts["contexts"][0]["snippets"][0]["path"] == "components/SurfacePanel.jsx"
    assert "repo_paths" not in contexts["contexts"][0]
    assert "repo_labels" not in contexts["contexts"][0]
    assert "hits" not in contexts["contexts"][0]
    assert contexts_with_hits["contexts"][0]["hits"][0]["repo_index"] == 1
    assert duplicate_contexts["shared_snippets"] is True
    assert duplicate_contexts["shared_snippet_count"] == 1
    assert duplicate_contexts["shared_selected_files"] == 1
    assert duplicate_contexts["contexts"][0]["snippet_refs"][0]["snippet_index"] == 0
    assert "snippets" not in duplicate_contexts["contexts"][0]
    assert duplicate_contexts["contexts"][0]["snippet_refs"] == duplicate_contexts["contexts"][1]["snippet_refs"]
    assert duplicate_contexts["contexts"][1]["snippet_refs"] == duplicate_contexts["contexts"][2]["snippet_refs"]
    assert workflow["mode"] == "workspace-context"
    assert workflow["queries"][0]["query"] == "SurfacePanel"
    assert workflow["queries"][0]["top_paths"] == ["ui:components/SurfacePanel.jsx"]
    assert "top_hits" not in workflow["queries"][0]
    assert workflow_full["queries"][0]["selected_paths"] == ["ui:components/SurfacePanel.jsx"]
    assert workflow_full["queries"][0]["top_hits"][0]["path"] == "components/SurfacePanel.jsx"


def test_mcp_workspace_context_many_rejects_empty_queries(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)

    try:
        workspace_context_many_tool(["", "   "], repos=[str(repo)])
    except ValueError as exc:
        assert "At least one non-empty query" in str(exc)
    else:
        raise AssertionError("workspace_context_many_tool should reject empty query batches")


def test_mcp_workspace_context_many_reuses_duplicate_query_packs(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    original_build = mcp_server.build_workspace_context_pack
    original_serialize = mcp_server.context_pack_to_dict
    calls: list[str] = []
    serialized_queries: list[str] = []
    usage_queries: list[str] = []

    def counting_build(*args, **kwargs):
        calls.append(str(args[1]))
        return original_build(*args, **kwargs)

    def counting_serialize(pack, *args, **kwargs):
        serialized_queries.append(pack.query)
        return original_serialize(pack, *args, **kwargs)

    def capture_usage(usages, *, tool: str) -> None:
        usage_queries.extend(f"{tool}:{query}:{pack.query}" for pack, query in usages)

    monkeypatch.setattr(mcp_server, "build_workspace_context_pack", counting_build)
    monkeypatch.setattr(mcp_server, "context_pack_to_dict", counting_serialize)
    monkeypatch.setattr(mcp_server, "_record_context_pack_usages", capture_usage)

    result = workspace_context_many_tool(["Service", " service ", "SERVICE"], repos=[str(repo)], max_files=1)

    assert result["query_count"] == 3
    assert result["unique_query_count"] == 1
    assert result["reused_query_count"] == 2
    assert calls == ["Service"]
    assert serialized_queries == ["Service"]
    assert usage_queries == ["workspace_context_many:Service:Service"]
    assert [context["query"] for context in result["contexts"]] == ["Service", "service", "SERVICE"]


def test_mcp_workspace_context_usage_skips_empty_workspace_repos(tmp_path: Path, monkeypatch) -> None:
    backend = _make_repo(tmp_path)
    ui = tmp_path / "ui"
    (ui / "components").mkdir(parents=True)
    (ui / "components/SurfacePanel.jsx").write_text(
        "export function SurfacePanel({ children }) {\n  return <section>{children}</section>;\n}\n"
    )
    build_catalog(backend)
    build_catalog(ui)
    events: list[tuple[str, list[dict[str, object]]]] = []

    def capture_events(store, usage_events: list[dict[str, object]]) -> None:
        repo_root = store.database_path.parent.parent
        events.append((str(repo_root), usage_events))

    monkeypatch.setattr(mcp_server.CatalogStore, "record_usage_events", capture_events)

    workspace_context_tool("SurfacePanel", repos=[str(backend), str(ui)], limit=5, max_files=1)

    assert len(events) == 1
    assert events[0][0] == str(ui.resolve())
    assert len(events[0][1]) == 1
    assert events[0][1][0]["query"] == "SurfacePanel"
    assert events[0][1][0]["result_count"] == 2
    assert events[0][1][0]["candidate_files"] == 1
    assert events[0][1][0]["returned_files"] == 1


def test_mcp_workflow_benchmark_can_exclude_test_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/constants.py").write_text("VALUE_CONFIGS = {'source': True}\n")
    (repo / "src/app/usage.py").write_text("print(VALUE_CONFIGS)\n")
    (repo / "tests/test_constants.py").write_text("VALUE_CONFIGS = {'test': True}\n")
    build_catalog(repo)

    result = workflow_benchmark_tool(
        ["VALUE_CONFIGS"],
        repos=[str(repo)],
        repeat=1,
        warmup=0,
        max_files=2,
        source_first=True,
        summary_only=False,
    )

    assert result["include_tests"] is False
    assert result["queries"][0]["selected_paths"] == ["src/app/constants.py"]
    assert result["queries"][0]["selected_files"] == 1


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/app/service.py").write_text("class Service:\n    pass\n")
    (repo / "src/app/api.py").write_text("from .service import Service\n")
    (repo / "src/app/extra.py").write_text("\n".join(f"VALUE_{index} = {index}" for index in range(60)))
    (repo / "tests/test_service.py").write_text("from src.app.service import Service\n")
    return repo
