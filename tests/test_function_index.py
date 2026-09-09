"""Function BM25 ranking, update and migration contracts."""

import sqlite3

import pytest

from code_intel.catalog_store import CatalogStore
from code_intel.catalog_upgrade import upgrade_catalog
from code_intel.cataloger import build_catalog
from code_intel.function_index import MAX_BODY_CHARS, identifier_words
from code_intel.lookup import lookup


def fixture_repo(tmp_path):
    (tmp_path / "src").mkdir()
    path = tmp_path / "src" / "cache.py"
    path.write_text(
        'def invalidateHTTPResponse(cache_key: str):\n    """Handle cache state.\n\n'
        '    Remove expired response entries from a cache namespace.\n    """\n'
        "    return cache_key\n"
    )
    build_catalog(tmp_path)
    return path, CatalogStore.for_repo(tmp_path)


def test_identifier_splitting_and_full_doc_ranking(tmp_path):
    _path, store = fixture_repo(tmp_path)
    assert "HTTP Response" in identifier_words("invalidateHTTPResponse")
    result = lookup(tmp_path, store, "expired response namespace", include_tests=False)
    assert result.hits[0].label == "function invalidateHTTPResponse"
    assert result.recovery_strategy == "function_bm25"
    with store.connect() as connection:
        row = connection.execute("SELECT doc,full_doc FROM symbols").fetchone()
        assert row["doc"] == "Handle cache state."
        assert "Remove expired" in row["full_doc"]


def test_incremental_replacement_and_delete_remove_stale_documents(tmp_path):
    path, store = fixture_repo(tmp_path)
    path.write_text('def renew_cache(value):\n    """Renew active cache entries."""\n    return value\n')
    build_catalog(tmp_path, incremental=True)
    assert not store.search_function_documents(["expired", "response"])
    assert lookup(tmp_path, store, "renew active entries").hits[0].label == "function renew_cache"
    path.unlink()
    build_catalog(tmp_path, incremental=True)
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM function_index").fetchone()[0] == 0


def test_upgrade_populates_function_index_and_preserves_usage(tmp_path):
    _path, store = fixture_repo(tmp_path)
    store.record_usage_event(tool="find", provider="catalog", estimated_saved_tokens=99)
    with store.connect() as connection:
        connection.execute("DROP TABLE function_index")
        connection.execute("ALTER TABLE symbols DROP COLUMN full_doc")
        connection.execute("PRAGMA user_version=1")
    assert upgrade_catalog(store.database_path)["backup_created"]
    assert store.usage_summary()["estimated_saved_tokens"] == 99
    assert lookup(tmp_path, store, "response namespace").hits
    assert not upgrade_catalog(store.database_path)["changed"]


def test_migration_failure_rolls_back_schema_and_keeps_backup(tmp_path, monkeypatch):
    _path, store = fixture_repo(tmp_path)
    with store.connect() as connection:
        connection.execute("DROP TABLE function_index")
        connection.execute("PRAGMA user_version=1")

    def fail(connection):
        connection.execute("CREATE TABLE temporary_migration_marker(value TEXT)")
        raise ValueError("synthetic failure")

    monkeypatch.setattr("code_intel.catalog_upgrade.migrate_function_index", fail)
    with pytest.raises(ValueError):
        upgrade_catalog(store.database_path)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='temporary_migration_marker'"
        ).fetchone()
    assert list((store.database_path.parent / ".backups").iterdir())


def test_exact_fast_path_and_body_bounds(tmp_path, monkeypatch):
    path, store = fixture_repo(tmp_path)
    path.write_text(
        path.read_text()
        + "\n"
        + 'def very_long_body():\n    """Long example."""\n'
        + "    value = '"
        + "x" * 16000
        + "'\n    return value\n"
    )
    build_catalog(tmp_path, incremental=True)
    with store.connect() as connection:
        assert connection.execute("SELECT MAX(length(body)) FROM function_index").fetchone()[0] <= MAX_BODY_CHARS

    def unexpected(*args, **kwargs):
        raise AssertionError("Exact identifiers must bypass BM25")

    monkeypatch.setattr(store, "search_function_documents", unexpected)
    assert lookup(tmp_path, store, "invalidateHTTPResponse").hits


def test_multiline_signature_excludes_comments_and_body(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def example(\n    value: dict[str, int],\n) -> bool:\n"
        "    # This is not part of the signature.\n    return bool(value)\n"
    )
    build_catalog(tmp_path)
    with CatalogStore.for_repo(tmp_path).connect() as connection:
        signature = connection.execute("SELECT signature FROM symbols").fetchone()[0]
    assert signature.endswith(") -> bool:")
    assert "not part" not in signature
    assert "return" not in signature


def test_test_documents_filtered_before_candidate_budget(tmp_path):
    _path, store = fixture_repo(tmp_path)
    (tmp_path / "testing").mkdir()
    (tmp_path / "testing" / "noise.py").write_text(
        "\n".join(f'def helper_{i}():\n    """Expired response namespace."""\n    return None\n' for i in range(80))
    )
    build_catalog(tmp_path, incremental=True)
    result = lookup(tmp_path, store, "expired response namespace", include_tests=False)
    assert result.hits[0].label == "function invalidateHTTPResponse"
    assert all(not hit.path.startswith("testing/") for hit in result.hits)
