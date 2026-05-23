"""Optional jCodemunch SQLite search provider."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JCODEMUNCH_HOME = ".code-index"


@dataclass(frozen=True, slots=True)
class JCodeMunchMatch:
    """A jCodemunch-backed symbol search result."""

    name: str
    qualified_name: str
    kind: str
    path: str
    line: int
    signature: str
    summary: str
    provider: str = "jcodemunch"


def find_jcodemunch_database(repo_path: str | Path) -> Path | None:
    """Find the jCodemunch SQLite database for ``repo_path``."""
    repo_root = Path(repo_path).resolve()
    code_index_dir = Path.home() / JCODEMUNCH_HOME
    if not code_index_dir.exists():
        return None

    for database_path in sorted(code_index_dir.glob("*.db")):
        try:
            source_root = _read_meta_value(database_path, "source_root")
        except sqlite3.Error:
            continue
        if not source_root:
            continue
        try:
            if Path(source_root).resolve() == repo_root:
                return database_path
        except OSError:
            continue
    return None


def search_jcodemunch_symbols(repo_path: str | Path, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search symbols in the local jCodemunch database when available."""
    database_path = find_jcodemunch_database(repo_path)
    if database_path is None:
        return []

    normalized = query.lower()
    like = f"%{normalized}%"
    bounded_limit = max(1, min(limit, 100))
    try:
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT name, qualified_name, kind, file, line, signature, summary
                FROM symbols
                WHERE lower(name) LIKE ?
                   OR lower(qualified_name) LIKE ?
                   OR lower(signature) LIKE ?
                   OR lower(summary) LIKE ?
                   OR lower(docstring) LIKE ?
                ORDER BY
                    CASE
                        WHEN lower(name) = ? THEN 0
                        WHEN lower(name) LIKE ? THEN 1
                        WHEN lower(qualified_name) LIKE ? THEN 2
                        ELSE 3
                    END,
                    file,
                    line
                LIMIT ?
                """,
                (like, like, like, like, like, normalized, f"{normalized}%", f"{normalized}%", bounded_limit),
            ).fetchall()
    except sqlite3.Error:
        return []

    return [
        {
            "name": row["name"] or "",
            "qualified_name": row["qualified_name"] or row["name"] or "",
            "kind": row["kind"] or "",
            "path": row["file"] or "",
            "line": int(row["line"] or 0),
            "signature": row["signature"] or "",
            "summary": row["summary"] or "",
            "provider": "jcodemunch",
        }
        for row in rows
    ]


def _read_meta_value(database_path: Path, key: str) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else ""
