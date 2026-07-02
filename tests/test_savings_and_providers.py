from __future__ import annotations

import sqlite3
from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.cli import main
from code_intel.jcodemunch_provider import search_jcodemunch_symbols
from code_intel.savings import estimate_saved_tokens_for_context_pack, estimate_saved_tokens_for_paths
from code_intel.symbol_search import search_symbols


def test_cli_records_lookup_savings(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)

    assert main(["scan", str(repo)]) == 0
    assert main(["find", "--repo", str(repo), "--provider", "catalog", "Service"]) == 0
    assert main(["savings", "--repo", str(repo)]) == 0

    captured = capsys.readouterr()
    assert "Usage events: 1" in captured.out
    assert "Estimated saved tokens:" in captured.out
    assert "find (catalog): 1 events" in captured.out


def test_cli_find_requires_scan_for_catalog(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)

    assert main(["find", "--repo", str(repo), "Service"]) == 1

    captured = capsys.readouterr()
    assert "Run `code-intel scan` first" in captured.err
    assert not (repo / ".code-intel/catalog.sqlite").exists()


def test_catalog_store_usage_summary(tmp_path: Path) -> None:
    store = CatalogStore.for_repo(tmp_path)

    store.record_usage_event(
        tool="find",
        provider="catalog",
        query="Service",
        result_count=2,
        candidate_files=50,
        returned_files=2,
        estimated_saved_tokens=12000,
    )

    summary = store.usage_summary()
    compact_summary = store.usage_summary(detailed=False)

    assert summary["events"] == 1
    assert summary["estimated_saved_tokens"] == 12000
    assert summary["candidate_files"] == 50
    assert summary["returned_files"] == 2
    assert summary["by_tool"][0]["tool"] == "find"
    assert summary["recent"][0]["query"] == "Service"
    assert compact_summary == {
        "events": 1,
        "estimated_saved_tokens": 12000,
        "candidate_files": 50,
        "returned_files": 2,
    }


def test_reusable_store_caches_usage_table_setup(tmp_path: Path) -> None:
    store = CatalogStore.for_repo(tmp_path, reuse_connection=True)
    statements: list[str] = []
    store.connect().set_trace_callback(statements.append)

    store.record_usage_events(
        [
            {
                "tool": "find",
                "provider": "catalog",
                "query": f"Service{index}",
                "result_count": 2,
                "candidate_files": 50,
                "returned_files": 2,
                "estimated_saved_tokens": 12000,
            }
            for index in range(3)
        ]
    )
    store.record_usage_event(
        tool="find",
        provider="catalog",
        query="Service3",
        result_count=2,
        candidate_files=50,
        returned_files=2,
        estimated_saved_tokens=12000,
    )

    summary = store.usage_summary(detailed=False)

    assert summary["events"] == 4
    assert sum(1 for statement in statements if "CREATE TABLE IF NOT EXISTS usage_events" in statement) == 1


def test_context_pack_savings_use_snippet_tokens(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src/app/noise.py").write_text("\n".join(f"NOISE_{index} = {index}" for index in range(200)))
    build_catalog(repo)
    store = CatalogStore.for_repo(repo)

    token_summary = store.file_token_summary(selected_paths={"src/app/service.py"})
    whole_file_metrics = estimate_saved_tokens_for_paths(store, {"src/app/service.py"}, result_count=1)
    snippet_metrics = estimate_saved_tokens_for_context_pack(
        store,
        {"src/app/service.py"},
        returned_tokens=5,
        result_count=1,
    )

    assert token_summary == {
        "file_count": 2,
        "total_tokens": 203 * 8,
        "selected_tokens": 3 * 8,
    }
    assert snippet_metrics["candidate_files"] == whole_file_metrics["candidate_files"]
    assert snippet_metrics["returned_files"] == whole_file_metrics["returned_files"]
    assert snippet_metrics["estimated_saved_tokens"] > whole_file_metrics["estimated_saved_tokens"]


def test_jcodemunch_provider_searches_matching_database(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    rows = search_jcodemunch_symbols(repo, "Service", limit=5)

    assert rows == [
        {
            "name": "Service",
            "qualified_name": "src.app.service.Service",
            "kind": "class",
            "path": "src/app/service.py",
            "line": 1,
            "signature": "class Service",
            "summary": "Runs service work.",
            "provider": "jcodemunch",
        }
    ]


def test_unified_search_defaults_to_catalog_even_when_jcodemunch_exists(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = search_symbols(repo, CatalogStore.for_repo(repo), "Service")

    assert result.provider == "catalog"
    assert result.symbols[0]["path"] == "src/app/service.py"


def test_unified_search_uses_jcodemunch_when_explicit(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    build_catalog(repo)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = search_symbols(repo, CatalogStore.for_repo(repo), "Service", provider="jcodemunch")

    assert result.provider == "jcodemunch"
    assert result.symbols[0]["summary"] == "Runs service work."


def test_cli_jcodemunch_provider_does_not_create_catalog(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_repo(tmp_path)
    _make_jcodemunch_database(tmp_path, repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert main(["find", "--repo", str(repo), "--provider", "jcodemunch", "Service"]) == 0

    captured = capsys.readouterr()
    assert "src/app/service.py" in captured.out
    assert not (repo / ".code-intel/catalog.sqlite").exists()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/app").mkdir(parents=True)
    (repo / "src/app/service.py").write_text("class Service:\n    def run(self) -> str:\n        return 'ok'\n")
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
        connection.execute("INSERT INTO meta(key, value) VALUES ('source_root', ?)", (str(repo.resolve()),))
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
