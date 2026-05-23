"""SQLite persistence for repository catalogs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_intel.models import Dependency, FileAnalysis, SourceFile, Symbol

DEFAULT_CATALOG_PATH = ".code-intel/catalog.sqlite"


class CatalogStore:
    """SQLite-backed repository catalog."""

    def __init__(self, database_path: Path) -> None:
        """Initialize the store for ``database_path``."""
        self.database_path = database_path

    @classmethod
    def for_repo(cls, repo_root: Path, database_path: Path | None = None) -> CatalogStore:
        """Create a store for ``repo_root``."""
        return cls((database_path or repo_root / DEFAULT_CATALOG_PATH).resolve())

    def connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with row dictionaries enabled."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def reset(self) -> None:
        """Drop and recreate all catalog tables."""
        with self.connect() as connection:
            connection.executescript(
                """
                DROP TABLE IF EXISTS meta;
                DROP TABLE IF EXISTS files;
                DROP TABLE IF EXISTS symbols;
                DROP TABLE IF EXISTS dependencies;

                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

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
                    end_line INTEGER,
                    signature TEXT NOT NULL,
                    doc TEXT NOT NULL,
                    exported INTEGER NOT NULL,
                    FOREIGN KEY(path) REFERENCES files(path)
                );

                CREATE TABLE dependencies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    import_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    resolved INTEGER NOT NULL
                );

                CREATE INDEX idx_symbols_name ON symbols(name);
                CREATE INDEX idx_symbols_path ON symbols(path);
                CREATE INDEX idx_dependencies_source ON dependencies(source_path);
                CREATE INDEX idx_dependencies_target ON dependencies(target_path);
                """
            )
            self._create_usage_tables(connection)

    def write_catalog(self, analyses: Iterable[FileAnalysis], metadata: dict[str, str]) -> None:
        """Persist a full catalog."""
        with self.connect() as connection:
            self._create_usage_tables(connection)
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                sorted(metadata.items()),
            )
            for analysis in analyses:
                self._insert_file(connection, analysis.source_file)
                self._insert_symbols(connection, analysis.symbols)
                self._insert_dependencies(connection, analysis.dependencies)

    def get_meta(self) -> dict[str, str]:
        """Return catalog metadata."""
        if not self.has_catalog():
            return {}
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def has_catalog(self) -> bool:
        """Return true when catalog tables are present."""
        if not self.database_path.exists():
            return False
        with self.connect() as connection:
            return self._table_exists(connection, "files") and self._table_exists(connection, "symbols")

    def file_count(self) -> int:
        """Return the number of cataloged files."""
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM files").fetchone()
        return int(row["count"])

    def symbol_count(self) -> int:
        """Return the number of cataloged symbols."""
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM symbols").fetchone()
        return int(row["count"])

    def dependency_count(self) -> int:
        """Return the number of cataloged dependencies."""
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM dependencies").fetchone()
        return int(row["count"])

    def search_symbols(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Search symbols by name, qualified name, signature, or docstring."""
        normalized = query.lower()
        like = f"%{normalized}%"
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM symbols
                WHERE lower(name) LIKE ?
                   OR lower(qualified_name) LIKE ?
                   OR lower(signature) LIKE ?
                   OR lower(doc) LIKE ?
                ORDER BY
                    CASE
                        WHEN lower(name) = ? THEN 0
                        WHEN lower(name) LIKE ? THEN 1
                        WHEN lower(qualified_name) LIKE ? THEN 2
                        ELSE 3
                    END,
                    path,
                    line
                LIMIT ?
                """,
                (like, like, like, like, normalized, f"{normalized}%", f"{normalized}%", limit),
            ).fetchall()

    def resolve_file_path(self, file_path: str) -> str | None:
        """Resolve a user-provided file path to a cataloged relative path."""
        normalized = Path(file_path).as_posix().lstrip("./")
        with self.connect() as connection:
            exact = connection.execute("SELECT path FROM files WHERE path = ?", (normalized,)).fetchone()
            if exact:
                return str(exact["path"])
            rows = connection.execute(
                "SELECT path FROM files WHERE path LIKE ? ORDER BY length(path)",
                (f"%{normalized}",),
            ).fetchall()
        if len(rows) == 1:
            return str(rows[0]["path"])
        return None

    def get_file(self, path: str) -> sqlite3.Row | None:
        """Return one indexed file row."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()

    def list_files(self) -> list[sqlite3.Row]:
        """Return all cataloged files."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM files ORDER BY path").fetchall()

    def list_internal_dependencies(self) -> list[sqlite3.Row]:
        """Return dependency rows that resolved to cataloged files."""
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT d.*
                FROM dependencies d
                JOIN files f ON f.path = d.target_path
                WHERE d.resolved = 1
                ORDER BY d.source_path, d.target_path
                """
            ).fetchall()

    def dependencies_for_file(self, path: str) -> list[sqlite3.Row]:
        """Return dependencies declared by ``path``."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM dependencies WHERE source_path = ? ORDER BY target_path",
                (path,),
            ).fetchall()

    def dependents_for_file(self, path: str) -> list[sqlite3.Row]:
        """Return files that directly depend on ``path``."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM dependencies WHERE target_path = ? AND resolved = 1 ORDER BY source_path",
                (path,),
            ).fetchall()

    def symbols_for_file(self, path: str) -> list[sqlite3.Row]:
        """Return symbols declared by ``path``."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM symbols WHERE path = ? ORDER BY line, name",
                (path,),
            ).fetchall()

    def record_usage_event(
        self,
        *,
        tool: str,
        provider: str,
        query: str = "",
        target_path: str = "",
        result_count: int = 0,
        candidate_files: int = 0,
        returned_files: int = 0,
        estimated_saved_tokens: int = 0,
    ) -> None:
        """Record one code-intel lookup event."""
        with self.connect() as connection:
            self._create_usage_tables(connection)
            connection.execute(
                """
                INSERT INTO usage_events(
                    created_at, tool, provider, query, target_path, result_count,
                    candidate_files, returned_files, estimated_saved_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    tool,
                    provider,
                    query,
                    target_path,
                    result_count,
                    candidate_files,
                    returned_files,
                    estimated_saved_tokens,
                ),
            )

    def usage_summary(self) -> dict[str, Any]:
        """Return aggregate code-intel usage and estimated savings."""
        if not self.database_path.exists():
            return _empty_usage_summary()
        with self.connect() as connection:
            if not self._table_exists(connection, "usage_events"):
                return _empty_usage_summary()
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS events,
                    COALESCE(SUM(estimated_saved_tokens), 0) AS estimated_saved_tokens,
                    COALESCE(SUM(candidate_files), 0) AS candidate_files,
                    COALESCE(SUM(returned_files), 0) AS returned_files
                FROM usage_events
                """
            ).fetchone()
            by_tool = connection.execute(
                """
                SELECT tool, provider, COUNT(*) AS events,
                       COALESCE(SUM(estimated_saved_tokens), 0) AS estimated_saved_tokens
                FROM usage_events
                GROUP BY tool, provider
                ORDER BY estimated_saved_tokens DESC, events DESC
                """
            ).fetchall()
            recent = connection.execute(
                """
                SELECT created_at, tool, provider, query, target_path, result_count,
                       estimated_saved_tokens
                FROM usage_events
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
        return {
            "events": int(totals["events"]),
            "estimated_saved_tokens": int(totals["estimated_saved_tokens"]),
            "candidate_files": int(totals["candidate_files"]),
            "returned_files": int(totals["returned_files"]),
            "by_tool": [dict(row) for row in by_tool],
            "recent": [dict(row) for row in recent],
        }

    def _insert_file(self, connection: sqlite3.Connection, source_file: SourceFile) -> None:
        connection.execute(
            "INSERT INTO files(path, language, line_count, size_bytes) VALUES (?, ?, ?, ?)",
            (source_file.path, source_file.language, source_file.line_count, source_file.size_bytes),
        )

    def _insert_symbols(self, connection: sqlite3.Connection, symbols: list[Symbol]) -> None:
        connection.executemany(
            """
            INSERT INTO symbols(name, qualified_name, kind, path, line, end_line, signature, doc, exported)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    symbol.name,
                    symbol.qualified_name,
                    symbol.kind,
                    symbol.path,
                    symbol.line,
                    symbol.end_line,
                    symbol.signature,
                    symbol.doc,
                    int(symbol.exported),
                )
                for symbol in symbols
            ],
        )

    def _insert_dependencies(self, connection: sqlite3.Connection, dependencies: list[Dependency]) -> None:
        connection.executemany(
            """
            INSERT INTO dependencies(source_path, target_path, import_name, kind, resolved)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    dependency.source_path,
                    dependency.target_path,
                    dependency.import_name,
                    dependency.kind,
                    int(dependency.resolved),
                )
                for dependency in dependencies
            ],
        )

    def _create_usage_tables(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                tool TEXT NOT NULL,
                provider TEXT NOT NULL,
                query TEXT NOT NULL,
                target_path TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                candidate_files INTEGER NOT NULL,
                returned_files INTEGER NOT NULL,
                estimated_saved_tokens INTEGER NOT NULL
            )
            """
        )

    def _table_exists(self, connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "events": 0,
        "estimated_saved_tokens": 0,
        "candidate_files": 0,
        "returned_files": 0,
        "by_tool": [],
        "recent": [],
    }
