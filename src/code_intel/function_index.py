"""Small field-weighted FTS5 index over bounded symbol documents."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

MAX_BODY_LINES = 120
MAX_BODY_CHARS = 8000
MAX_DOC_CHARS = 2000
FUNCTION_INDEX_SQL = """CREATE VIRTUAL TABLE IF NOT EXISTS function_index USING fts5(
    path UNINDEXED, name, signature, doc, body, tokenize='porter unicode61 remove_diacritics 2'
)"""


def identifier_words(value: str, *, retain_original: bool = True) -> str:
    """Preserve identifiers while adding snake/camel/acronym word boundaries."""
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated).replace("_", " ")
    if not retain_original:
        return separated
    return value if separated == value else f"{value} {separated}"


def index_file(connection: sqlite3.Connection, path: str, lines: dict[int, str] | None = None) -> None:
    """Index one file from redacted catalog content within the caller's transaction.

    Args:
        connection: Catalog connection; rows must support string keys.
        path: Exact cataloged relative path.
        lines: Already-redacted analysis lines, or None to load existing lines.
    """
    if lines is None:
        lines = {
            row["line"]: row["content"]
            for row in connection.execute("SELECT line,content FROM text_lines WHERE path=?", (path,))
        }
    rows = connection.execute("SELECT * FROM symbols WHERE path=? ORDER BY id", (path,)).fetchall()
    documents = []
    for row in rows:
        start = row["line"]
        end = min(row["end_line"] or start, start + MAX_BODY_LINES - 1)
        body = ""
        if row["kind"] in ("function", "method", "component", "hook"):
            body = "\n".join(lines.get(number, "") for number in range(start, end + 1))[:MAX_BODY_CHARS]
        documents.append(
            (
                row["id"],
                path,
                identifier_words(row["qualified_name"]),
                identifier_words(row["signature"]),
                (row["full_doc"] or row["doc"])[:MAX_DOC_CHARS],
                identifier_words(body, retain_original=False)[:MAX_BODY_CHARS],
            )
        )
    connection.executemany(
        "INSERT INTO function_index(rowid,path,name,signature,doc,body) VALUES(?,?,?,?,?,?)", documents
    )


def migrate_function_index(connection: sqlite3.Connection) -> None:
    """Backfill a legacy catalog from stored data without dropping usage history."""
    if "full_doc" not in {row[1] for row in connection.execute("PRAGMA table_info(symbols)")}:
        connection.execute("ALTER TABLE symbols ADD COLUMN full_doc TEXT NOT NULL DEFAULT ''")
    connection.execute(FUNCTION_INDEX_SQL)
    connection.execute("DELETE FROM function_index")
    for row in connection.execute("SELECT path FROM files ORDER BY path").fetchall():
        index_file(connection, row[0])


def search_functions(
    connection: sqlite3.Connection, terms: list[str], *, limit: int, include_body: bool, include_tests: bool
) -> list[dict[str, Any]]:
    """Rank at most 64 BM25 candidates with multiple distinct matching terms.

    Args:
        connection: Catalog connection.
        terms: Up to eight normalized alphanumeric terms.
        limit: Maximum returned symbols, capped at 64.
        include_body: Include function bodies in matching when true.
        include_tests: Include test-file symbols when true.

    Returns:
        Symbol dictionaries ordered by weighted BM25 rank.
    """
    terms = list(dict.fromkeys(terms))[:8]
    if len(terms) < 2 or any(not re.fullmatch(r"[a-z0-9]{3,48}", term) for term in terms):
        return []
    fields = "" if include_body else "{name signature doc}: "
    queries = [fields + f'"{term}"' for term in terms]
    query = " OR ".join(queries)
    coverage = " + ".join(
        "(symbol_id IN (SELECT rowid FROM function_index WHERE function_index MATCH ?))" for _ in terms
    )
    test_clause = (
        ""
        if include_tests
        else (
            " AND ('/'||s.path) NOT GLOB '*/tests/*' AND ('/'||s.path) NOT GLOB '*/test_*' "
            "AND ('/'||s.path) NOT GLOB '*/testing/*' AND ('/'||s.path) NOT GLOB '*/__tests__/*' "
            "AND ('/'||s.path) NOT GLOB '*/test/*' "
            "AND s.path NOT GLOB '*.test.*' AND s.path NOT GLOB '*_test.py'"
        )
    )
    rows = connection.execute(
        "WITH matched AS (SELECT rowid AS symbol_id, rank FROM function_index "
        f"WHERE function_index MATCH ? AND rank MATCH 'bm25(0,8,3,5,1)' "
        f"{test_clause.replace('s.path', 'function_index.path')} ORDER BY rank LIMIT 64) "
        f"SELECT s.*, matched.rank AS bm25_rank FROM matched JOIN symbols s ON s.id=matched.symbol_id "
        f"WHERE ({coverage})>=? ORDER BY matched.rank,s.path,s.line LIMIT ?",
        (query, *queries, max(2, (len(terms) + 2) // 3), max(1, min(limit, 64))),
    ).fetchall()
    return [dict(row) for row in rows]
