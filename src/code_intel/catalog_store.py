"""SQLite persistence for repository catalogs."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_intel.function_index import FUNCTION_INDEX_SQL, index_file, search_functions
from code_intel.models import Dependency, FileAnalysis, SourceFile, Symbol, TextLine, TextMatch

DEFAULT_CATALOG_PATH = ".code-intel/catalog.sqlite"
CATALOG_SCHEMA_VERSION = 2
SQLITE_PARAMETER_CHUNK_SIZE = 500
CATALOG_WRITE_LOCK_TIMEOUT_SECONDS = 30.0
REUSABLE_CONNECTION_OPEN_ATTEMPTS = 3
type FileSearchRow = tuple[sqlite3.Row, str, str, str, str]
type DatabaseSignature = tuple[int, int, int, int, int] | None
type DatabaseIdentity = tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class CatalogCounts:
    """Aggregate row counts for a repository catalog.

    Attributes:
        file_count: Number of cataloged source files.
        symbol_count: Number of indexed symbols.
        dependency_count: Number of indexed dependency edges.
        text_line_count: Number of indexed source text lines.
    """

    file_count: int
    symbol_count: int
    dependency_count: int
    text_line_count: int


class CatalogStore:
    """SQLite-backed repository catalog."""

    def __init__(self, database_path: Path, *, reuse_connection: bool = False) -> None:
        """Initialize a SQLite catalog store.

        Args:
            database_path: SQLite database path for the catalog.
            reuse_connection: Whether read-heavy callers should reuse one
                connection for the lifetime of this store.
        """
        self.database_path = database_path
        self.reuse_connection = reuse_connection
        self._connection: sqlite3.Connection | None = None
        self._has_catalog_cache: bool | None = None
        self._table_exists_cache: dict[str, bool] = {}
        self._column_exists_cache: dict[tuple[str, str], bool] = {}
        self._usage_tables_ready = False
        self._file_rows_cache: list[FileSearchRow] | None = None
        self._file_row_by_path_cache: dict[str, sqlite3.Row | None] = {}
        self._symbols_by_file_cache: dict[str, list[sqlite3.Row]] = {}
        self._source_line_range_cache: dict[tuple[str, int, int], dict[int, str]] = {}
        self._file_token_total_cache: dict[int, tuple[int, int]] = {}
        self._selected_token_cache: dict[tuple[int, tuple[str, ...]], int] = {}
        self._database_signature = _database_signature(database_path)
        self._data_version: int | None = None

    @classmethod
    def for_repo(
        cls,
        repo_root: Path,
        database_path: Path | None = None,
        *,
        reuse_connection: bool = False,
    ) -> CatalogStore:
        """Create a store for a repository catalog.

        Args:
            repo_root: Repository root containing `.code-intel/catalog.sqlite`.
            database_path: Optional explicit catalog database path.
            reuse_connection: Whether this store should reuse one SQLite
                connection across method calls.

        Returns:
            Catalog store for the repository.
        """
        return cls((database_path or repo_root / DEFAULT_CATALOG_PATH).resolve(), reuse_connection=reuse_connection)

    def connect(self) -> sqlite3.Connection:
        """Open or return a SQLite connection with row dictionaries enabled.

        Returns:
            SQLite connection configured with `sqlite3.Row` row factories.
        """
        if not self.reuse_connection:
            return self._open_connection()
        self._refresh_reusable_state()
        if self._connection is None:
            connection, signature, data_version = self._open_reusable_connection()
            self._connection = connection
            self._database_signature = signature
            self._data_version = data_version
        return self._connection

    def close(self) -> None:
        """Close the reusable SQLite connection, when one is open."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._database_signature = _database_signature(self.database_path)
        self._data_version = None
        self._clear_schema_cache()
        self._clear_file_cache()

    def _open_connection(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= schema_version <= CATALOG_SCHEMA_VERSION:
            connection.close()
            raise ValueError("Unsupported catalog schema; upgrade the package before accessing this database")
        connection.row_factory = sqlite3.Row
        return connection

    def _open_reusable_connection(self) -> tuple[sqlite3.Connection, DatabaseSignature, int]:
        for _attempt in range(REUSABLE_CONNECTION_OPEN_ATTEMPTS):
            identity_before_open = _database_identity(self.database_path)
            connection = self._open_connection()
            signature_after_open = _database_signature(self.database_path)
            identity_after_open = _identity_from_signature(signature_after_open)
            if identity_before_open == identity_after_open:
                return connection, signature_after_open, _sqlite_data_version(connection)
            connection.close()
        raise RuntimeError(f"Catalog changed repeatedly while opening {self.database_path}")

    def reset(self) -> None:
        """Drop and recreate all catalog tables."""
        self._clear_schema_cache()
        self._clear_file_cache()
        with self.connect() as connection:
            connection.executescript(
                """
                DROP TABLE IF EXISTS meta;
                DROP TABLE IF EXISTS files;
                DROP TABLE IF EXISTS symbols;
                DROP TABLE IF EXISTS function_index;
                DROP TABLE IF EXISTS dependencies;
                DROP TABLE IF EXISTS text_index;
                DROP TABLE IF EXISTS text_lines;

                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE files (
                    path TEXT PRIMARY KEY,
                    language TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    modified_ns INTEGER NOT NULL DEFAULT 0
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
                    full_doc TEXT NOT NULL DEFAULT '',
                    exported INTEGER NOT NULL,
                    FOREIGN KEY(path) REFERENCES files(path)
                );

                CREATE TABLE dependencies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    import_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    resolved INTEGER NOT NULL,
                    category TEXT NOT NULL DEFAULT 'code'
                );

                CREATE TABLE text_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(path, line),
                    FOREIGN KEY(path) REFERENCES files(path)
                );

                CREATE VIRTUAL TABLE text_index USING fts5(
                    path UNINDEXED,
                    line UNINDEXED,
                    content,
                    content='text_lines',
                    content_rowid='id',
                    tokenize='trigram'
                );
                """
            )
            self._create_usage_tables(connection)
            connection.execute(FUNCTION_INDEX_SQL)
            connection.execute(f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}")
        self._clear_schema_cache()
        self._clear_file_cache()
        self._mark_reusable_state_current()

    def publish_catalog(self, analyses: Iterable[FileAnalysis], metadata: dict[str, str]) -> None:
        """Build, validate, and atomically publish a complete catalog.

        The replacement catalog is constructed beside the live database so a
        failed analysis write or validation leaves the last usable catalog
        untouched. Catalog writers are serialized during the final handoff, and
        current usage history is copied immediately before replacement.

        Args:
            analyses: Complete source analyses for the replacement catalog.
            metadata: Metadata key/value pairs for the replacement catalog.

        Raises:
            RuntimeError: If the temporary catalog fails count or SQLite
                integrity validation.
            OSError: If the temporary database cannot be created or atomically
                moved into place.
            sqlite3.Error: If SQLite cannot write the catalog or copy usage
                history during publication.
        """
        analysis_list = list(analyses)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.database_path.name}.",
            suffix=".tmp",
            dir=self.database_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        temporary_store = CatalogStore(temporary_path)
        published = False
        try:
            temporary_store.reset()
            temporary_store.write_catalog(analysis_list, metadata)
            expected_counts = CatalogCounts(
                file_count=len(analysis_list),
                symbol_count=sum(len(analysis.symbols) for analysis in analysis_list),
                dependency_count=sum(len(analysis.dependencies) for analysis in analysis_list),
                text_line_count=sum(len(analysis.text_lines) for analysis in analysis_list),
            )
            actual_counts = temporary_store.catalog_counts()
            if actual_counts != expected_counts:
                raise RuntimeError(
                    f"Temporary catalog count validation failed: expected {expected_counts}, got {actual_counts}"
                )

            validation_connection = sqlite3.connect(temporary_path)
            try:
                integrity_row = validation_connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                validation_connection.close()
            if integrity_row is None or str(integrity_row[0]).casefold() != "ok":
                detail = "missing result" if integrity_row is None else str(integrity_row[0])
                raise RuntimeError(f"Temporary catalog integrity validation failed: {detail}")

            temporary_store.close()
            with _catalog_write_lock(self.database_path):
                _copy_usage_events(self.database_path, temporary_path)
                self.close()
                os.replace(temporary_path, self.database_path)
                published = True
                self._database_signature = _database_signature(self.database_path)
                self._clear_schema_cache()
                self._clear_file_cache()
        finally:
            temporary_store.close()
            if not published:
                temporary_path.unlink(missing_ok=True)

    def write_catalog(self, analyses: Iterable[FileAnalysis], metadata: dict[str, str]) -> None:
        """Persist a full catalog."""
        with self.connect() as connection:
            self._configure_write_connection(connection)
            self._create_usage_tables(connection)
            self._clear_schema_cache()
            self._clear_file_cache()
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                sorted(metadata.items()),
            )
            text_index_paths: set[str] = set()
            for analysis in analyses:
                self._insert_file(connection, analysis.source_file)
                self._insert_symbols(connection, analysis.symbols)
                self._insert_dependencies(connection, analysis.dependencies)
                self._insert_text_lines(connection, analysis.text_lines)
                index_file(
                    connection, analysis.source_file.path, {row.line: row.content for row in analysis.text_lines}
                )
                if analysis.text_lines:
                    text_index_paths.add(analysis.source_file.path)
            self._insert_text_index_paths(connection, text_index_paths)
            self._create_catalog_indexes(connection)
        self._mark_reusable_state_current()

    def write_incremental_update(
        self,
        analyses: Iterable[FileAnalysis],
        removed_paths: set[str],
        metadata: dict[str, str],
    ) -> None:
        """Apply a partial catalog update for changed, added, or removed files.

        Args:
            analyses: Complete analyses for files whose catalog rows should be
                replaced or inserted.
            removed_paths: Cataloged source paths that no longer exist.
            metadata: Metadata key/value pairs to upsert after the update.
        """
        analysis_list = list(analyses)
        replaced_paths = {analysis.source_file.path for analysis in analysis_list}
        paths_to_delete = replaced_paths | set(removed_paths)
        with _catalog_write_lock(self.database_path):
            with self.connect() as connection:
                self._configure_write_connection(connection)
                self._create_usage_tables(connection)
                self._clear_schema_cache()
                self._clear_file_cache()
                self._delete_file_rows(connection, paths_to_delete)
                for analysis in analysis_list:
                    self._insert_file(connection, analysis.source_file)
                    self._insert_symbols(connection, analysis.symbols)
                    self._insert_dependencies(connection, analysis.dependencies)
                    self._insert_text_lines(connection, analysis.text_lines)
                    index_file(
                        connection, analysis.source_file.path, {row.line: row.content for row in analysis.text_lines}
                    )
                self._insert_text_index_paths(connection, replaced_paths)
                self._create_catalog_indexes(connection)
                self._upsert_meta(connection, metadata)
            self._clear_file_cache()
            self._mark_reusable_state_current()

    def update_meta(self, metadata: dict[str, str]) -> None:
        """Update catalog metadata without rewriting source analysis tables.

        Args:
            metadata: Metadata key/value pairs to upsert.
        """
        with _catalog_write_lock(self.database_path):
            with self.connect() as connection:
                self._upsert_meta(connection, metadata)
            self._mark_reusable_state_current()

    def get_meta(self) -> dict[str, str]:
        """Return catalog metadata."""
        if not self.has_catalog():
            return {}
        with self.connect() as connection:
            if not self._table_exists(connection, "meta"):
                return {}
            rows = connection.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def has_catalog(self) -> bool:
        """Return true when catalog tables are present."""
        self._refresh_reusable_state()
        if self.reuse_connection and self._has_catalog_cache is not None:
            return self._has_catalog_cache
        if not self.database_path.exists():
            if self.reuse_connection:
                self._has_catalog_cache = False
            return False
        with self.connect() as connection:
            has_catalog = self._table_exists(connection, "files") and self._table_exists(connection, "symbols")
        if self.reuse_connection:
            self._has_catalog_cache = has_catalog
        return has_catalog

    def supports_incremental_catalog(self) -> bool:
        """Return true when the catalog has file fingerprints for reuse.

        Returns:
            True when the files table includes the columns required for
            incremental scan decisions.
        """
        if not self.has_catalog():
            return False
        with self.connect() as connection:
            file_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(files)").fetchall()}
        return {"content_hash", "modified_ns"}.issubset(file_columns)

    def supports_dependency_categories(self) -> bool:
        """Return true when dependency rows include category metadata.

        Returns:
            True when the dependencies table includes the category column used
            to distinguish code, standard-library, external package, asset, and
            unresolved edges.
        """
        if not self.has_catalog():
            return False
        with self.connect() as connection:
            return self._table_has_column(connection, "dependencies", "category")

    def supports_text_index(self) -> bool:
        """Return true when the catalog has source text search rows.

        Returns:
            True when the FTS-backed text index table exists.
        """
        if not self.has_catalog():
            return False
        with self.connect() as connection:
            return self._table_exists(connection, "text_index") and self._table_exists(connection, "text_lines")

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
            if not self._table_exists(connection, "dependencies"):
                return 0
            row = connection.execute("SELECT COUNT(*) AS count FROM dependencies").fetchone()
        return int(row["count"])

    def text_line_count(self) -> int:
        """Return the number of source lines in the text index.

        Returns:
            Indexed source line count, or 0 when the catalog predates the text
            index schema.
        """
        if not self.supports_text_index():
            return 0
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM text_lines").fetchone()
        return int(row["count"])

    def catalog_counts(self) -> CatalogCounts:
        """Return aggregate catalog row counts using one SQLite connection.

        Returns:
            Counts for files, symbols, dependency edges, and indexed text lines.
            Missing optional tables are reported as zero for older catalogs.
        """
        if not self.has_catalog():
            return CatalogCounts(file_count=0, symbol_count=0, dependency_count=0, text_line_count=0)
        with self.connect() as connection:
            try:
                row = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM files) AS file_count,
                        (SELECT COUNT(*) FROM symbols) AS symbol_count,
                        (SELECT COUNT(*) FROM dependencies) AS dependency_count,
                        (SELECT COUNT(*) FROM text_lines) AS text_line_count
                    """
                ).fetchone()
            except sqlite3.OperationalError:
                return self._catalog_counts_compatible(connection)
            return CatalogCounts(
                file_count=int(row["file_count"]),
                symbol_count=int(row["symbol_count"]),
                dependency_count=int(row["dependency_count"]),
                text_line_count=int(row["text_line_count"]),
            )

    def _catalog_counts_compatible(self, connection: sqlite3.Connection) -> CatalogCounts:
        file_count = _table_count(connection, "files")
        symbol_count = _table_count(connection, "symbols")
        dependency_count = (
            _table_count(connection, "dependencies") if self._table_exists(connection, "dependencies") else 0
        )
        text_line_count = _table_count(connection, "text_lines") if self._table_exists(connection, "text_lines") else 0
        return CatalogCounts(
            file_count=file_count,
            symbol_count=symbol_count,
            dependency_count=dependency_count,
            text_line_count=text_line_count,
        )

    def search_symbols(self, query: str, limit: int = 20, *, include_fuzzy: bool = True) -> list[sqlite3.Row]:
        """Search symbols by name, qualified name, signature, or docstring.

        Args:
            query: Symbol query string.
            limit: Maximum rows to return.
            include_fuzzy: Whether to search slower signature/docstring
                contains matches after exact and prefix name matches.

        Returns:
            Matching symbol rows ordered by match quality and source location.
        """
        stripped = query.strip()
        prefix = f"{stripped}%"
        contains = f"%{stripped}%"
        bounded_limit = max(1, min(limit, 500))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM symbols
                WHERE name = ? COLLATE NOCASE
                   OR qualified_name = ? COLLATE NOCASE
                   OR name LIKE ? COLLATE NOCASE
                   OR qualified_name LIKE ? COLLATE NOCASE
                ORDER BY
                    CASE
                        WHEN name = ? COLLATE NOCASE THEN 0
                        WHEN qualified_name = ? COLLATE NOCASE THEN 1
                        WHEN name LIKE ? COLLATE NOCASE THEN 2
                        ELSE 5
                    END,
                    path,
                    line
                LIMIT ?
                """,
                (stripped, stripped, prefix, prefix, stripped, stripped, prefix, bounded_limit),
            ).fetchall()
            if len(rows) >= bounded_limit or not include_fuzzy:
                return rows

            seen_ids = {int(row["id"]) for row in rows}
            fallback_rows = connection.execute(
                """
                SELECT *
                FROM symbols
                WHERE name LIKE ? COLLATE NOCASE
                   OR qualified_name LIKE ? COLLATE NOCASE
                   OR signature LIKE ? COLLATE NOCASE
                   OR doc LIKE ? COLLATE NOCASE
                ORDER BY
                    CASE
                        WHEN name = ? COLLATE NOCASE THEN 0
                        WHEN qualified_name = ? COLLATE NOCASE THEN 1
                        WHEN name LIKE ? COLLATE NOCASE THEN 2
                        WHEN qualified_name LIKE ? COLLATE NOCASE THEN 3
                        ELSE 3
                    END,
                    path,
                    line
                LIMIT ?
                """,
                (
                    contains,
                    contains,
                    contains,
                    contains,
                    stripped,
                    stripped,
                    prefix,
                    prefix,
                    bounded_limit * 2,
                ),
            ).fetchall()
        combined = [*rows]
        for row in fallback_rows:
            row_id = int(row["id"])
            if row_id not in seen_ids:
                seen_ids.add(row_id)
                combined.append(row)
            if len(combined) >= bounded_limit:
                break
        return combined

    def search_symbol_prefixes(self, queries: list[str], limit: int = 20) -> list[sqlite3.Row]:
        """Search symbols for several exact or prefix query variants.

        Args:
            queries: Query variants to match against symbol names and qualified
                names.
            limit: Maximum rows to return.

        Returns:
            Matching symbol rows ordered by variant order, match quality, and
            source location.
        """
        variants = _normalized_query_variants(queries)
        if not variants:
            return []
        bounded_limit = max(1, min(limit, 500))
        clauses: list[str] = []
        params: list[str] = []
        rank_clauses: list[str] = []
        rank_params: list[str] = []
        for index, variant in enumerate(variants):
            prefix = f"{variant}%"
            clauses.append(
                """
                (
                    name = ? COLLATE NOCASE
                    OR qualified_name = ? COLLATE NOCASE
                    OR name LIKE ? COLLATE NOCASE
                    OR qualified_name LIKE ? COLLATE NOCASE
                )
                """
            )
            params.extend([variant, variant, prefix, prefix])
            rank_clauses.extend(
                [
                    f"WHEN name = ? COLLATE NOCASE THEN {index * 4}",
                    f"WHEN qualified_name = ? COLLATE NOCASE THEN {index * 4 + 1}",
                    f"WHEN name LIKE ? COLLATE NOCASE THEN {index * 4 + 2}",
                    f"WHEN qualified_name LIKE ? COLLATE NOCASE THEN {index * 4 + 3}",
                ]
            )
            rank_params.extend([variant, variant, prefix, prefix])

        with self.connect() as connection:
            return connection.execute(
                f"""
                SELECT *
                FROM symbols
                WHERE {" OR ".join(clauses)}
                ORDER BY
                    CASE
                        {" ".join(rank_clauses)}
                        ELSE {len(variants) * 4}
                    END,
                    path,
                    line
                LIMIT ?
                """,
                (*params, *rank_params, bounded_limit),
            ).fetchall()

    def list_searchable_symbols(self, limit: int = 20) -> list[sqlite3.Row]:
        """Return representative public symbols for generated benchmark queries.

        Args:
            limit: Maximum number of symbols to return.

        Returns:
            Public symbols ordered by source path and line number.
        """
        bounded_limit = max(1, min(limit, 500))
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM symbols
                WHERE exported = 1
                  AND name != ''
                  AND kind IN ('component', 'hook', 'class', 'function', 'method', 'constant', 'type')
                GROUP BY name
                ORDER BY
                    CASE kind
                        WHEN 'component' THEN 0
                        WHEN 'hook' THEN 1
                        WHEN 'class' THEN 2
                        WHEN 'function' THEN 3
                        WHEN 'method' THEN 4
                        ELSE 3
                    END,
                    path,
                    line
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()

    def keyword_symbol_candidates(self, terms: list[str], *, include_body: bool = True) -> list[dict[str, Any]]:
        """Collect bounded metadata and innermost-symbol source evidence.

        Args:
            terms: At most eight alphanumeric keywords, each at least three characters.
            include_body: Whether to query the text index for source evidence.

        Returns:
            At most 320 symbol dictionaries: 128 metadata candidates plus at
            most 24 source-line candidates per term. Queries are parameterized.
        """
        terms = list(dict.fromkeys(terms))[:8]
        if not terms or any(not re.fullmatch(r"[a-z0-9]{3,48}", term) for term in terms):
            return []
        expression = " + ".join(
            "(instr(lower(qualified_name || ' ' || signature || ' ' || doc), ?) > 0)" for _ in terms
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT *, ({expression}) AS coverage FROM symbols WHERE coverage > 0 "
                "ORDER BY coverage DESC, path, line LIMIT 128",
                tuple(terms),
            ).fetchall()
            candidates = {row["id"]: {**dict(row), "body_terms": []} for row in rows}
            if include_body and self.supports_text_index():
                matches = [
                    (row["path"], row["line"], term)
                    for term in terms
                    for row in _search_text_rows(connection, term, 24)
                ]
                if matches:
                    placeholders = ",".join("(?,?,?)" for _ in matches)
                    evidence = connection.execute(
                        f"WITH matches(path,line,term) AS (VALUES {placeholders}) "
                        "SELECT s.*, m.term FROM matches m JOIN symbols s ON s.id = "
                        "(SELECT id FROM symbols WHERE path=m.path AND line<=m.line "
                        "AND COALESCE(end_line,line)>=m.line "
                        "ORDER BY COALESCE(end_line,line)-line, id LIMIT 1)",
                        tuple(value for match in matches for value in match),
                    ).fetchall()
                    for row in evidence:
                        candidate = candidates.setdefault(row["id"], {**dict(row), "body_terms": []})
                        if row["term"] not in candidate["body_terms"]:
                            candidate["body_terms"].append(row["term"])
        return list(candidates.values())

    def search_function_documents(
        self, terms: list[str], *, limit: int = 20, include_body: bool = True, include_tests: bool = True
    ) -> list[dict[str, Any]] | None:
        """Return weighted BM25 symbols, or None when the catalog needs upgrading."""
        with self.connect() as connection:
            if not self._table_exists(connection, "function_index"):
                return None
            return search_functions(
                connection, terms, limit=limit, include_body=include_body, include_tests=include_tests
            )

    def search_files(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        """Search cataloged file paths by path, basename, or stem.

        Args:
            query: File path fragment, basename, or stem.
            limit: Maximum files to return.

        Returns:
            Matching cataloged file rows ordered by path specificity.
        """
        stripped = query.strip().lstrip("./")
        if not stripped:
            return []
        bounded_limit = max(1, min(limit, 500))
        contains = f"%{stripped}%"
        basename = Path(stripped).name
        basename_suffix = f"%/{basename}"
        basename_with_extension = f"%/{basename}.%"
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM files
                WHERE path = ? COLLATE NOCASE
                   OR path LIKE ? COLLATE NOCASE
                   OR path LIKE ? COLLATE NOCASE
                   OR path LIKE ? COLLATE NOCASE
                ORDER BY
                    CASE
                        WHEN path = ? COLLATE NOCASE THEN 0
                        WHEN path LIKE ? COLLATE NOCASE THEN 1
                        WHEN path LIKE ? COLLATE NOCASE THEN 2
                        ELSE 3
                    END,
                    length(path),
                    path
                LIMIT ?
                """,
                (
                    stripped,
                    basename_suffix,
                    basename_with_extension,
                    contains,
                    stripped,
                    basename_suffix,
                    basename_with_extension,
                    bounded_limit,
                ),
            ).fetchall()

    def search_files_many(self, queries: list[str], limit: int = 20) -> list[sqlite3.Row]:
        """Search cataloged file paths for several query variants.

        Args:
            queries: Query variants to match against source paths, basenames, or
                stems.
            limit: Maximum files to return.

        Returns:
            Matching file rows ordered by variant order, path specificity, and
            source path.
        """
        variants = _normalized_query_variants([query.lstrip("./") for query in queries])
        if not variants:
            return []
        bounded_limit = max(1, min(limit, 500))
        if self.reuse_connection:
            return self._search_files_many_cached(variants, limit=bounded_limit)
        clauses: list[str] = []
        params: list[str] = []
        for variant in variants:
            basename = Path(variant).name
            clauses.append(
                """
                (
                    path = ? COLLATE NOCASE
                    OR path LIKE ? COLLATE NOCASE
                    OR path LIKE ? COLLATE NOCASE
                    OR path LIKE ? COLLATE NOCASE
                )
                """
            )
            params.extend([variant, f"%/{basename}", f"%/{basename}.%", f"%{variant}%"])

        candidate_limit = min(500, max(bounded_limit * len(variants) * 5, bounded_limit))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM files
                WHERE {" OR ".join(clauses)}
                ORDER BY length(path), path
                LIMIT ?
                """,
                (*params, candidate_limit),
            ).fetchall()
        return sorted(rows, key=lambda row: _file_variant_score(row, variants))[:bounded_limit]

    def _search_files_many_cached(self, variants: list[str], *, limit: int) -> list[sqlite3.Row]:
        variant_keys = [
            (normalized, Path(normalized).name)
            for variant in variants
            for normalized in [variant.casefold().lstrip("./")]
        ]
        scored_rows = [
            (score, row)
            for row, path, normalized_path, basename, stem in self._cached_file_rows()
            if (
                score := _cached_file_variant_score(
                    path=path,
                    normalized_path=normalized_path,
                    basename=basename,
                    stem=stem,
                    variants=variant_keys,
                )
            )[1]
            < 9
        ]
        return [row for _score, row in sorted(scored_rows, key=lambda item: item[0])[:limit]]

    def _cached_file_rows(self) -> list[FileSearchRow]:
        self._refresh_reusable_state()
        if self._file_rows_cache is None:
            with self.connect() as connection:
                rows = connection.execute("SELECT * FROM files ORDER BY path").fetchall()
            self._file_rows_cache = [
                (row, path, path.casefold(), parsed.name.casefold(), parsed.stem.casefold())
                for row in rows
                for path in [str(row["path"])]
                for parsed in [Path(path)]
            ]
        return self._file_rows_cache

    def search_text(self, query: str, limit: int = 20, context_lines: int = 1) -> list[TextMatch]:
        """Search indexed source text and return bounded line snippets.

        Args:
            query: Source text fragment to find.
            limit: Maximum matches to return.
            context_lines: Number of lines before and after each match to
                include in the snippet.

        Returns:
            Text matches ordered by the SQLite FTS rank, then path and line.

        Raises:
            RuntimeError: If the catalog was created before text search support.
            ValueError: If the query is empty.
        """
        stripped = query.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        if not self.supports_text_index():
            raise RuntimeError(f"Catalog at {self.database_path} lacks a text index. Run `code-intel scan` first.")

        bounded_limit = max(1, min(limit, 500))
        bounded_context = max(0, min(context_lines, 10))
        with self.connect() as connection:
            rows = _search_text_rows(connection, stripped, bounded_limit)
            matches = [_text_match_from_row(connection, row, context_lines=bounded_context) for row in rows]
        return matches

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
        self._refresh_reusable_state()
        if self.reuse_connection and path in self._file_row_by_path_cache:
            return self._file_row_by_path_cache[path]
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        if self.reuse_connection:
            self._file_row_by_path_cache[path] = row
        return row

    def source_line_range(self, path: str, start_line: int, end_line: int) -> dict[int, str]:
        """Return indexed source lines for a bounded line range.

        Args:
            path: Cataloged repository-relative source path.
            start_line: One-based first line to return.
            end_line: One-based final line to return.

        Returns:
            Mapping from one-based line number to indexed line content. Blank or
            whitespace-only lines may be absent because the text index stores
            non-empty source lines.
        """
        self._refresh_reusable_state()
        bounded_start = max(1, start_line)
        bounded_end = max(bounded_start, end_line)
        cache_key = (path, bounded_start, bounded_end)
        cached = self._source_line_range_cache.get(cache_key) if self.reuse_connection else None
        if cached is not None:
            return dict(cached)
        with self.connect() as connection:
            if not self._table_exists(connection, "text_lines"):
                return {}
            rows = connection.execute(
                """
                SELECT line, content
                FROM text_lines
                WHERE path = ?
                  AND line BETWEEN ? AND ?
                ORDER BY line
                """,
                (path, bounded_start, bounded_end),
            ).fetchall()
        result = {int(row["line"]): str(row["content"]) for row in rows}
        if self.reuse_connection:
            self._source_line_range_cache[cache_key] = result
        return dict(result)

    def list_files(self) -> list[sqlite3.Row]:
        """Return all cataloged files."""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM files ORDER BY path").fetchall()

    def file_token_summary(self, *, selected_paths: set[str] | None = None, tokens_per_line: int = 8) -> dict[str, int]:
        """Return aggregate file and token counts without loading file rows.

        Args:
            selected_paths: Optional cataloged paths whose whole-file token
                counts should be reported separately.
            tokens_per_line: Directional token estimate assigned to each source
                line.

        Returns:
            Summary with ``file_count``, ``total_tokens``, and
            ``selected_tokens``.
        """
        self._refresh_reusable_state()
        selected = selected_paths or set()
        multiplier = max(1, tokens_per_line)
        selected_key = tuple(sorted(selected))
        total_cache = self._file_token_total_cache.get(multiplier) if self.reuse_connection else None
        selected_cache_key = (multiplier, selected_key)
        selected_tokens_cache = self._selected_token_cache.get(selected_cache_key) if self.reuse_connection else None
        with self.connect() as connection:
            if total_cache is None:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS file_count,
                           COALESCE(SUM(CASE WHEN line_count < 1 THEN 1 ELSE line_count END), 0) AS line_count
                    FROM files
                    """
                ).fetchone()
                file_count = int(row["file_count"])
                total_tokens = int(row["line_count"]) * multiplier
                if self.reuse_connection:
                    self._file_token_total_cache[multiplier] = (file_count, total_tokens)
            else:
                file_count, total_tokens = total_cache

            if selected_tokens_cache is None:
                selected_lines = 0
                for path_chunk in _chunks(selected_key, SQLITE_PARAMETER_CHUNK_SIZE):
                    placeholders = ", ".join("?" for _ in path_chunk)
                    selected_row = connection.execute(
                        f"""
                        SELECT COALESCE(SUM(CASE WHEN line_count < 1 THEN 1 ELSE line_count END), 0) AS line_count
                        FROM files
                        WHERE path IN ({placeholders})
                        """,
                        tuple(path_chunk),
                    ).fetchone()
                    selected_lines += int(selected_row["line_count"])
                selected_tokens = selected_lines * multiplier
                if self.reuse_connection:
                    self._selected_token_cache[selected_cache_key] = selected_tokens
            else:
                selected_tokens = selected_tokens_cache
        return {
            "file_count": file_count,
            "total_tokens": total_tokens,
            "selected_tokens": selected_tokens,
        }

    def symbol_counts_by_file(self) -> dict[str, int]:
        """Return symbol counts keyed by cataloged file path.

        Returns:
            Mapping from repository-relative source path to declared symbol
            count. Files without symbols are omitted.
        """
        with self.connect() as connection:
            if not self._table_exists(connection, "symbols"):
                return {}
            rows = connection.execute(
                """
                SELECT path, COUNT(*) AS count
                FROM symbols
                GROUP BY path
                ORDER BY path
                """
            ).fetchall()
        return {str(row["path"]): int(row["count"]) for row in rows}

    def load_file_analysis(self, path: str) -> FileAnalysis | None:
        """Return complete catalog analysis for one file.

        Args:
            path: Cataloged repository-relative file path.

        Returns:
            File analysis reconstructed from SQLite rows, or None when the file
            is not present in the catalog.
        """
        with self.connect() as connection:
            file_row = connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
            if file_row is None:
                return None
            symbol_rows = connection.execute(
                "SELECT * FROM symbols WHERE path = ? ORDER BY line, name",
                (path,),
            ).fetchall()
            dependency_rows = connection.execute(
                "SELECT * FROM dependencies WHERE source_path = ? ORDER BY target_path, import_name",
                (path,),
            ).fetchall()
            text_line_rows = connection.execute(
                "SELECT * FROM text_lines WHERE path = ? ORDER BY line",
                (path,),
            ).fetchall()
            has_dependency_category = self._table_has_column(connection, "dependencies", "category")
        return FileAnalysis(
            source_file=SourceFile(
                path=str(file_row["path"]),
                language=str(file_row["language"]),
                line_count=int(file_row["line_count"]),
                size_bytes=int(file_row["size_bytes"]),
                content_hash=str(file_row["content_hash"] or ""),
                modified_ns=int(file_row["modified_ns"] or 0),
            ),
            symbols=[
                Symbol(
                    name=str(row["name"]),
                    qualified_name=str(row["qualified_name"]),
                    kind=str(row["kind"]),
                    path=str(row["path"]),
                    line=int(row["line"]),
                    end_line=int(row["end_line"]) if row["end_line"] is not None else None,
                    signature=str(row["signature"]),
                    doc=str(row["doc"]),
                    exported=bool(row["exported"]),
                    full_doc=str(row["full_doc"]) if "full_doc" in row.keys() else "",
                )
                for row in symbol_rows
            ],
            dependencies=[
                Dependency(
                    source_path=str(row["source_path"]),
                    target_path=str(row["target_path"]),
                    import_name=str(row["import_name"]),
                    kind=str(row["kind"]),
                    resolved=bool(row["resolved"]),
                    category=str(row["category"]) if has_dependency_category else "code",
                )
                for row in dependency_rows
            ],
            text_lines=[
                TextLine(
                    path=str(row["path"]),
                    line=int(row["line"]),
                    content=str(row["content"]),
                )
                for row in text_line_rows
            ],
        )

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

    def dependency_sources_for_targets(self, target_paths: set[str]) -> set[str]:
        """Return source files that currently depend on any target path.

        Args:
            target_paths: Cataloged dependency target paths.

        Returns:
            Source paths with dependency rows pointing at the supplied targets.
        """
        if not target_paths:
            return set()
        placeholders = ", ".join("?" for _ in target_paths)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT source_path
                FROM dependencies
                WHERE target_path IN ({placeholders})
                ORDER BY source_path
                """,
                tuple(sorted(target_paths)),
            ).fetchall()
        return {str(row["source_path"]) for row in rows}

    def sources_with_unresolved_dependencies(self) -> set[str]:
        """Return source files with unresolved relative/internal imports.

        Returns:
            Source paths whose dependency rows may resolve differently after a
            source file is added.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT source_path
                FROM dependencies
                WHERE resolved = 0
                  AND category = 'unresolved'
                ORDER BY source_path
                """
            ).fetchall()
        return {str(row["source_path"]) for row in rows}

    def symbols_for_file(self, path: str) -> list[sqlite3.Row]:
        """Return symbols declared by ``path``."""
        self._refresh_reusable_state()
        cached = self._symbols_by_file_cache.get(path) if self.reuse_connection else None
        if cached is not None:
            return list(cached)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM symbols WHERE path = ? ORDER BY line, name",
                (path,),
            ).fetchall()
        if self.reuse_connection:
            self._symbols_by_file_cache[path] = rows
        return list(rows)

    def dependency_summary(self, limit: int = 20) -> dict[str, Any]:
        """Return aggregate dependency category counts and examples.

        Args:
            limit: Maximum number of example imports to include for each
                non-code category.

        Returns:
            Dictionary with total dependency counts, per-category counts, and
            small example lists for external packages, static assets, and
            unresolved imports.
        """
        bounded_limit = max(1, min(limit, 100))
        empty = {
            "total": 0,
            "resolved": 0,
            "code": 0,
            "stdlib": 0,
            "external": 0,
            "asset": 0,
            "unresolved": 0,
            "supports_categories": False,
            "stdlib_imports": [],
            "external_packages": [],
            "asset_imports": [],
            "unresolved_imports": [],
        }
        if not self.has_catalog():
            return empty
        with self.connect() as connection:
            if not self._table_exists(connection, "dependencies"):
                return empty
            has_category = self._table_has_column(connection, "dependencies", "category")
            category_expr = "category" if has_category else "CASE WHEN resolved = 1 THEN 'code' ELSE 'unresolved' END"
            rows = connection.execute(
                f"""
                SELECT {category_expr} AS category, resolved, COUNT(*) AS count
                FROM dependencies
                GROUP BY category, resolved
                """
            ).fetchall()
            examples = {
                "stdlib_imports": _example_rows(connection, category_expr, "stdlib", bounded_limit),
                "external_packages": _example_rows(connection, category_expr, "external", bounded_limit),
                "asset_imports": _example_rows(connection, category_expr, "asset", bounded_limit),
                "unresolved_imports": _example_rows(connection, category_expr, "unresolved", bounded_limit),
            }

        summary = dict(empty)
        summary["supports_categories"] = has_category
        for row in rows:
            category = str(row["category"])
            count = int(row["count"])
            summary["total"] += count
            if bool(row["resolved"]):
                summary["resolved"] += count
            if category in {"code", "stdlib", "external", "asset", "unresolved"}:
                summary[category] += count
        summary.update(examples)
        return summary

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
        self.record_usage_events(
            [
                {
                    "tool": tool,
                    "provider": provider,
                    "query": query,
                    "target_path": target_path,
                    "result_count": result_count,
                    "candidate_files": candidate_files,
                    "returned_files": returned_files,
                    "estimated_saved_tokens": estimated_saved_tokens,
                }
            ]
        )

    def record_usage_events(self, events: list[dict[str, Any]]) -> None:
        """Record several code-intel lookup events in one transaction.

        Args:
            events: Usage event payloads with the same keys accepted by
                ``record_usage_event``.
        """
        if not events:
            return
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        with _catalog_write_lock(self.database_path):
            with self.connect() as connection:
                self._create_usage_tables(connection)
                connection.executemany(
                    """
                    INSERT INTO usage_events(
                        created_at, tool, provider, query, target_path, result_count,
                        candidate_files, returned_files, estimated_saved_tokens
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            created_at,
                            str(event.get("tool", "")),
                            str(event.get("provider", "")),
                            str(event.get("query", "")),
                            str(event.get("target_path", "")),
                            int(event.get("result_count", 0)),
                            int(event.get("candidate_files", 0)),
                            int(event.get("returned_files", 0)),
                            int(event.get("estimated_saved_tokens", 0)),
                        )
                        for event in events
                    ],
                )
            self._mark_reusable_state_current()

    def usage_summary(self, *, detailed: bool = True) -> dict[str, Any]:
        """Return aggregate code-intel usage and estimated savings.

        Args:
            detailed: When true, include per-tool totals and recent events.

        Returns:
            Usage counters and estimated context savings.
        """
        if not self.database_path.exists():
            return _empty_usage_summary(detailed=detailed)
        with self.connect() as connection:
            if not self._table_exists(connection, "usage_events"):
                return _empty_usage_summary(detailed=detailed)
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
            summary = {
                "events": int(totals["events"]),
                "estimated_saved_tokens": int(totals["estimated_saved_tokens"]),
                "candidate_files": int(totals["candidate_files"]),
                "returned_files": int(totals["returned_files"]),
            }
            if not detailed:
                return summary

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
            """
            INSERT INTO files(path, language, line_count, size_bytes, content_hash, modified_ns)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_file.path,
                source_file.language,
                source_file.line_count,
                source_file.size_bytes,
                source_file.content_hash,
                source_file.modified_ns,
            ),
        )

    def _insert_symbols(self, connection: sqlite3.Connection, symbols: list[Symbol]) -> None:
        connection.executemany(
            """
            INSERT INTO symbols(name, qualified_name, kind, path, line, end_line, signature, doc, exported, full_doc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    symbol.full_doc,
                )
                for symbol in symbols
            ],
        )

    def _insert_dependencies(self, connection: sqlite3.Connection, dependencies: list[Dependency]) -> None:
        connection.executemany(
            """
            INSERT INTO dependencies(source_path, target_path, import_name, kind, resolved, category)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    dependency.source_path,
                    dependency.target_path,
                    dependency.import_name,
                    dependency.kind,
                    int(dependency.resolved),
                    dependency.category,
                )
                for dependency in dependencies
            ],
        )

    def _insert_text_lines(self, connection: sqlite3.Connection, text_lines: list[TextLine]) -> None:
        if not text_lines:
            return
        rows = [(line.path, line.line, line.content) for line in text_lines]
        connection.executemany(
            """
            INSERT INTO text_lines(path, line, content)
            VALUES (?, ?, ?)
            """,
            rows,
        )

    def _delete_file_rows(self, connection: sqlite3.Connection, paths: set[str]) -> None:
        if not paths:
            return
        path_rows = [(path,) for path in sorted(paths)]
        if self._table_exists(connection, "function_index"):
            connection.executemany(
                "DELETE FROM function_index WHERE rowid IN (SELECT id FROM symbols WHERE path=?)", path_rows
            )
        connection.executemany("DELETE FROM symbols WHERE path = ?", path_rows)
        connection.executemany("DELETE FROM dependencies WHERE source_path = ?", path_rows)
        self._delete_text_index_paths(connection, paths)
        connection.executemany("DELETE FROM text_lines WHERE path = ?", path_rows)
        connection.executemany("DELETE FROM files WHERE path = ?", path_rows)

    def _upsert_meta(self, connection: sqlite3.Connection, metadata: dict[str, str]) -> None:
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            sorted(metadata.items()),
        )

    def _rebuild_text_index(self, connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO text_index(text_index) VALUES ('rebuild')")

    def _insert_text_index_paths(self, connection: sqlite3.Connection, paths: set[str]) -> None:
        for path_chunk in _chunks(sorted(paths), SQLITE_PARAMETER_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in path_chunk)
            connection.execute(
                f"""
                INSERT INTO text_index(rowid, path, line, content)
                SELECT id, path, line, content
                FROM text_lines
                WHERE path IN ({placeholders})
                """,
                tuple(path_chunk),
            )

    def _delete_text_index_paths(self, connection: sqlite3.Connection, paths: set[str]) -> None:
        for path_chunk in _chunks(sorted(paths), SQLITE_PARAMETER_CHUNK_SIZE):
            placeholders = ", ".join("?" for _ in path_chunk)
            connection.execute(
                f"""
                DELETE FROM text_index
                WHERE rowid IN (
                    SELECT id
                    FROM text_lines
                    WHERE path IN ({placeholders})
                )
                """,
                tuple(path_chunk),
            )

    def _create_usage_tables(self, connection: sqlite3.Connection) -> None:
        if self.reuse_connection and self._usage_tables_ready:
            return
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
        if self.reuse_connection:
            self._usage_tables_ready = True

    def _create_catalog_indexes(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_symbols_name_nocase ON symbols(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_symbols_qualified_name_nocase ON symbols(qualified_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
            CREATE INDEX IF NOT EXISTS idx_dependencies_source ON dependencies(source_path);
            CREATE INDEX IF NOT EXISTS idx_dependencies_target ON dependencies(target_path);
            CREATE INDEX IF NOT EXISTS idx_dependencies_category ON dependencies(category);
            CREATE INDEX IF NOT EXISTS idx_text_lines_content ON text_lines(content COLLATE NOCASE);
            """
        )

    def _configure_write_connection(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode = MEMORY")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA temp_store = MEMORY")

    def _refresh_reusable_state(self) -> None:
        if not self.reuse_connection:
            return
        current_signature = _database_signature(self.database_path)
        if current_signature != self._database_signature:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._database_signature = current_signature
            self._data_version = None
            self._clear_schema_cache()
            self._clear_file_cache()
            return
        if self._connection is None:
            return

        current_data_version = _sqlite_data_version(self._connection)
        if self._data_version is None:
            self._data_version = current_data_version
            return
        if current_data_version != self._data_version:
            self._data_version = current_data_version
            self._clear_schema_cache()
            self._clear_file_cache()

    def _mark_reusable_state_current(self) -> None:
        if not self.reuse_connection:
            return
        self._database_signature = _database_signature(self.database_path)
        self._data_version = _sqlite_data_version(self._connection) if self._connection is not None else None

    def _clear_schema_cache(self) -> None:
        self._has_catalog_cache = None
        self._table_exists_cache.clear()
        self._column_exists_cache.clear()
        self._usage_tables_ready = False

    def _clear_file_cache(self) -> None:
        self._file_rows_cache = None
        self._file_row_by_path_cache.clear()
        self._symbols_by_file_cache.clear()
        self._source_line_range_cache.clear()
        self._file_token_total_cache.clear()
        self._selected_token_cache.clear()

    def _table_exists(self, connection: sqlite3.Connection, table_name: str) -> bool:
        cached = self._table_exists_cache.get(table_name) if self.reuse_connection else None
        if cached is not None:
            return cached
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        exists = row is not None
        if self.reuse_connection:
            self._table_exists_cache[table_name] = exists
        return exists

    def _table_has_column(self, connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
        cache_key = (table_name, column_name)
        cached = self._column_exists_cache.get(cache_key) if self.reuse_connection else None
        if cached is not None:
            return cached
        exists = any(str(row["name"]) == column_name for row in connection.execute(f"PRAGMA table_info({table_name})"))
        if self.reuse_connection:
            self._column_exists_cache[cache_key] = exists
        return exists


def _database_signature(database_path: Path) -> DatabaseSignature:
    try:
        stat = database_path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _database_identity(database_path: Path) -> DatabaseIdentity:
    try:
        stat = database_path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino)


def _identity_from_signature(signature: DatabaseSignature) -> DatabaseIdentity:
    if signature is None:
        return None
    return (signature[0], signature[1])


def _sqlite_data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    return int(row[0]) if row is not None else 0


@contextmanager
def _catalog_write_lock(database_path: Path) -> Iterator[None]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(f".{database_path.name}.write-lock.sqlite")
    timeout_ms = int(CATALOG_WRITE_LOCK_TIMEOUT_SECONDS * 1000)
    connection = sqlite3.connect(
        lock_path,
        timeout=CATALOG_WRITE_LOCK_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        connection.execute("BEGIN EXCLUSIVE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        connection.close()


def _copy_usage_events(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists() or source_path == destination_path:
        return
    connection = sqlite3.connect(destination_path)
    try:
        connection.execute("ATTACH DATABASE ? AS live_catalog", (str(source_path),))
        if connection.execute("PRAGMA live_catalog.user_version").fetchone()[0] > CATALOG_SCHEMA_VERSION:
            raise ValueError("Cannot replace a catalog created by a newer package")
        try:
            usage_table = connection.execute(
                """
                SELECT 1
                FROM live_catalog.sqlite_master
                WHERE type = 'table' AND name = 'usage_events'
                """
            ).fetchone()
            if usage_table is None:
                return
            connection.execute(
                """
                INSERT INTO main.usage_events(
                    id, created_at, tool, provider, query, target_path,
                    result_count, candidate_files, returned_files,
                    estimated_saved_tokens
                )
                SELECT
                    id, created_at, tool, provider, query, target_path,
                    result_count, candidate_files, returned_files,
                    estimated_saved_tokens
                FROM live_catalog.usage_events
                ORDER BY id
                """
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.execute("DETACH DATABASE live_catalog")
    finally:
        connection.close()


def _example_rows(
    connection: sqlite3.Connection,
    category_expr: str,
    category: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT source_path, import_name, target_path, kind, COUNT(*) AS count
        FROM dependencies
        WHERE {category_expr} = ?
        GROUP BY source_path, import_name, target_path, kind
        ORDER BY count DESC, target_path, source_path
        LIMIT ?
        """,
        (category, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _normalized_query_variants(queries: list[str]) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()
    for query in queries:
        stripped = query.strip()
        normalized = stripped.casefold()
        if not stripped or normalized in seen:
            continue
        seen.add(normalized)
        variants.append(stripped)
    return variants


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    return int(row["count"])


def _file_variant_score(row: sqlite3.Row, variants: list[str]) -> tuple[int, int, int, str]:
    path = str(row["path"])
    normalized_path = path.casefold()
    parsed = Path(path)
    basename = parsed.name.casefold()
    stem = parsed.stem.casefold()
    for index, variant in enumerate(variants):
        normalized = variant.casefold().lstrip("./")
        variant_basename = Path(normalized).name
        if normalized_path == normalized:
            return (index, 0, len(path), path)
        if basename == normalized or stem == normalized:
            return (index, 1, len(path), path)
        if normalized_path.endswith(f"/{variant_basename}"):
            return (index, 2, len(path), path)
        if normalized in normalized_path:
            return (index, 3, len(path), path)
    return (len(variants), 9, len(path), path)


def _cached_file_variant_score(
    *,
    path: str,
    normalized_path: str,
    basename: str,
    stem: str,
    variants: list[tuple[str, str]],
) -> tuple[int, int, int, str]:
    for index, (normalized, variant_basename) in enumerate(variants):
        if normalized_path == normalized:
            return (index, 0, len(path), path)
        if basename == normalized or stem == normalized:
            return (index, 1, len(path), path)
        if normalized_path.endswith(f"/{variant_basename}"):
            return (index, 2, len(path), path)
        if normalized in normalized_path:
            return (index, 3, len(path), path)
    return (len(variants), 9, len(path), path)


def _search_text_rows(connection: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    if len(query) < 3:
        return _search_text_rows_like(connection, query, limit)
    try:
        candidate_limit = max(limit, min(limit * 5, 500))
        rows = connection.execute(
            """
            SELECT text_index.path AS path,
                   text_index.line AS line,
                   text_index.content AS content,
                   files.language AS language
            FROM text_index
            JOIN files ON files.path = text_index.path
            WHERE text_index MATCH ?
            ORDER BY rank, text_index.path, text_index.line
            LIMIT ?
            """,
            (_fts_phrase_query(query), candidate_limit),
        ).fetchall()
        return sorted(
            rows,
            key=lambda row: _text_row_score(query, row),
        )[:limit]
    except sqlite3.OperationalError:
        return _search_text_rows_like(connection, query, limit)


def _search_text_rows_like(connection: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT text_lines.path AS path,
               text_lines.line AS line,
               text_lines.content AS content,
               files.language AS language
        FROM text_lines
        JOIN files ON files.path = text_lines.path
        WHERE text_lines.content LIKE ? COLLATE NOCASE
        ORDER BY text_lines.path, text_lines.line
        LIMIT ?
        """,
        (f"%{query}%", limit),
    ).fetchall()


def _text_match_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    context_lines: int,
) -> TextMatch:
    path = str(row["path"])
    line = int(row["line"])
    start_line = max(1, line - context_lines)
    end_line = line + context_lines
    context_rows = connection.execute(
        """
        SELECT line, content
        FROM text_lines
        WHERE path = ?
          AND line BETWEEN ? AND ?
        ORDER BY line
        """,
        (path, start_line, end_line),
    ).fetchall()
    if context_rows:
        start_line = int(context_rows[0]["line"])
        end_line = int(context_rows[-1]["line"])
    snippet = "\n".join(f"{int(context_row['line'])}: {context_row['content']}" for context_row in context_rows)
    return TextMatch(
        path=path,
        line=line,
        language=str(row["language"]),
        content=str(row["content"]),
        snippet=snippet,
        start_line=start_line,
        end_line=end_line,
    )


def _fts_phrase_query(query: str) -> str:
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


def _empty_usage_summary(*, detailed: bool = True) -> dict[str, Any]:
    summary = {
        "events": 0,
        "estimated_saved_tokens": 0,
        "candidate_files": 0,
        "returned_files": 0,
    }
    if detailed:
        summary["by_tool"] = []
        summary["recent"] = []
    return summary


def _text_row_score(query: str, row: sqlite3.Row) -> tuple[int, str, int]:
    path = str(row["path"])
    content = str(row["content"]).strip()
    normalized_query = query.casefold()
    normalized_content = content.casefold()
    path_penalty = 1000 if _is_test_path(path) else 0
    if _looks_like_definition(content, query):
        base_score = 0
    elif normalized_content.startswith(normalized_query):
        base_score = 100
    elif re.search(rf"\bimport\b.*\b{re.escape(query)}\b", content):
        base_score = 200
    elif normalized_query in normalized_content:
        base_score = 400
    else:
        base_score = 800
    return (path_penalty + base_score, path, int(row["line"]))


def _looks_like_definition(content: str, query: str) -> bool:
    return bool(re.match(rf"^{re.escape(query)}\b\s*(?::|=)", content))


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name
    return "tests" in parts or name.startswith("test_") or ".test." in name or name.endswith("_test.py")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
