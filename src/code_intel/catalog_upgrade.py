"""Explicit transactional catalog version migration with SQLite backups."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from code_intel.catalog_store import CATALOG_SCHEMA_VERSION, _catalog_write_lock
from code_intel.config_upgrade import backup_path
from code_intel.function_index import migrate_function_index


def _preflight(connection: sqlite3.Connection) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= CATALOG_SCHEMA_VERSION:
        raise ValueError("Future catalog schema; upgrade the package first")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"files", "symbols", "dependencies", "meta", "text_lines", "usage_events"}.issubset(tables):
        raise ValueError("Unsupported legacy catalog; use scan to rebuild its index")
    if version == CATALOG_SCHEMA_VERSION and (
        "function_index" not in tables
        or "full_doc" not in {row[1] for row in connection.execute("PRAGMA table_info(symbols)")}
    ):
        raise ValueError("Current catalog lacks its function index; run scan to rebuild")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Catalog integrity check failed")
    return version


def upgrade_catalog(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Stamp a compatible legacy catalog without removing data or history.

    Args:
        path: Existing repository catalog; missing databases are reported only.
        dry_run: Open read-only and validate without writing.

    Returns:
        Anonymous schema transition and backup status.

    Raises:
        ValueError: On future schemas, unsupported tables or failed integrity.
        sqlite3.Error: When backup or migration fails; transactions roll back.
    """
    if not path.exists():
        return {"status": "missing", "changed": False, "backup_created": False}
    if path.is_symlink():
        raise ValueError("Catalog symlinks cannot be migrated")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        version = _preflight(connection)
    finally:
        connection.close()
    result = {
        "status": "current" if version == CATALOG_SCHEMA_VERSION else "upgrade",
        "from_version": version,
        "to_version": CATALOG_SCHEMA_VERSION,
        "changed": version != CATALOG_SCHEMA_VERSION,
        "backup_created": False,
    }
    if dry_run or version == CATALOG_SCHEMA_VERSION:
        return result
    with _catalog_write_lock(path):
        source = sqlite3.connect(path)
        source.row_factory = sqlite3.Row
        try:
            current = _preflight(source)
            if current == CATALOG_SCHEMA_VERSION:
                return {**result, "status": "current", "changed": False}
            backup = backup_path(path)
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
            finally:
                destination.close()
            with source:
                source.execute("BEGIN IMMEDIATE")
                migrate_function_index(source)
                source.execute(f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}")
            result["backup_created"] = True
        finally:
            source.close()
    return result
