"""Combined symbol and source-text lookup for agent navigation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from code_intel.catalog_store import CatalogStore
from code_intel.models import TextMatch
from code_intel.query_recovery import QUERY_GUIDANCE, query_terms, rank_keyword_candidates
from code_intel.symbol_search import search_symbols

LookupKind = Literal["symbol", "file", "text"]
DEFAULT_EXACT_SYMBOL_TEXT_LIMIT = 3
DEFAULT_EXACT_FILE_TEXT_LIMIT = 3
MAX_LOOKUP_QUERY_VARIANTS = 6
TEXT_HIT_SCORE_BASE = 2000
TEXT_HIT_SCORE_FLOOR = 1500
UI_ROUTE_PATH_MARKERS = (
    "config/navigation",
    "config/routes",
    "router",
    "routes",
)
UI_PAGE_PATH_MARKERS = (
    "/pages/",
    "pages/",
)
UI_METADATA_LINE_MARKERS = (
    "label",
    "title",
    "shortlabel",
    "description",
)


@dataclass(frozen=True, slots=True)
class LookupHit:
    """One combined lookup hit.

    Attributes:
        kind: Whether the hit came from symbol metadata or source text.
        path: Cataloged repository-relative file path.
        line: One-based source line number.
        label: Human-readable match label.
        detail: Signature, line content, or short supporting detail.
        score: Lower scores are ranked earlier.
        end_line: Optional one-based ending line for symbol hits.
        summary: Bounded indexed docstring summary for symbol hits.
    """

    kind: LookupKind
    path: str
    line: int
    label: str
    detail: str
    score: int
    end_line: int | None = None
    summary: str = ""


@dataclass(frozen=True, slots=True)
class LookupResult:
    """Combined lookup result for one query.

    Attributes:
        query: User query string.
        repo_path: Resolved repository path.
        symbols: Number of symbol hits considered.
        files: Number of file hits considered.
        text_matches: Number of text hits considered.
        hits: Ranked combined hits.
        recovery_terms: Lexical terms tried only after the original query missed.
    """

    query: str
    repo_path: str
    symbols: int
    files: int
    text_matches: int
    hits: list[LookupHit]
    recovery_terms: tuple[str, ...] = ()


def lookup(
    repo_path: str | Path,
    store: CatalogStore,
    query: str,
    *,
    limit: int = 20,
    symbol_limit: int | None = None,
    file_limit: int | None = None,
    text_limit: int | None = None,
    fallback_text_limit: int | None = None,
    context_lines: int = 0,
    include_tests: bool = True,
    include_fuzzy_symbols: bool | None = None,
) -> LookupResult:
    """Search symbols and source text together.

    Args:
        repo_path: Repository path being searched.
        store: Catalog store for the repository.
        query: Prefer an identifier or short fragment. Sentences use a bounded
            keyword fallback only after exact/phrase lookup misses.
        limit: Maximum combined hits to return.
        symbol_limit: Optional maximum symbol hits to consider. Use 0 to skip
            symbol lookup.
        file_limit: Optional maximum file path hits to consider. Use 0 to skip
            file lookup.
        text_limit: Optional maximum text hits to consider.
        fallback_text_limit: Optional text hit budget used only when normal text
            search is disabled and symbol/file lookup finds no hits.
        context_lines: Number of nearby lines to include in text snippets.
        include_tests: Whether test files may be returned as lookup hits.
        include_fuzzy_symbols: Whether to run broader symbol signature/docstring
            scans after exact/prefix symbol variants miss. Defaults to false for
            source-first, text-disabled lookups and true otherwise. Explicit
            false also disables keyword recovery.

    Returns:
        Ranked combined lookup response.

    Raises:
        ValueError: If the query is empty.
        RuntimeError: If text search is requested against an old catalog schema.
    """
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must not be empty")

    bounded_limit = max(1, min(limit, 100))
    bounded_symbol_limit = (
        0 if symbol_limit == 0 else _candidate_limit(limit=symbol_limit or bounded_limit, include_tests=include_tests)
    )
    bounded_file_limit = (
        0 if file_limit == 0 else _candidate_limit(limit=file_limit or bounded_limit, include_tests=include_tests)
    )
    bounded_context = max(0, min(context_lines, 10))
    allow_fuzzy_symbols = include_fuzzy_symbols
    if allow_fuzzy_symbols is None:
        allow_fuzzy_symbols = not (text_limit == 0 and not include_tests)

    query_variants = _query_variants(stripped)
    raw_symbol_rows = (
        []
        if bounded_symbol_limit == 0
        else _search_symbol_rows(
            repo_path,
            store,
            query_variants,
            limit=bounded_symbol_limit,
            include_fuzzy=allow_fuzzy_symbols,
        )
    )
    raw_file_rows = (
        [] if bounded_file_limit == 0 else _search_file_rows(store, query_variants, limit=bounded_file_limit)
    )
    symbol_rows = _filter_rows_for_tests(raw_symbol_rows, include_tests=include_tests)
    file_rows = _filter_rows_for_tests(raw_file_rows, include_tests=include_tests)
    default_text_limit = _default_text_limit(
        stripped,
        symbol_rows=symbol_rows,
        file_rows=file_rows,
        limit=bounded_limit,
    )
    bounded_text_limit = (
        max(0, min(text_limit, 100)) if text_limit is not None else max(1, min(default_text_limit, 100))
    )
    if bounded_text_limit == 0 and fallback_text_limit is not None and not symbol_rows and not file_rows:
        bounded_text_limit = max(0, min(fallback_text_limit, 100))
    text_matches = (
        []
        if bounded_text_limit == 0
        else _filter_text_matches_for_tests(
            store.search_text(
                stripped,
                limit=_candidate_limit(limit=bounded_text_limit, include_tests=include_tests),
                context_lines=bounded_context,
            ),
            include_tests=include_tests,
        )
    )
    hits = _rank_hits(
        query=stripped,
        symbol_rows=symbol_rows,
        file_rows=file_rows,
        text_matches=text_matches,
        limit=bounded_limit,
    )
    recovery_terms: list[str] = []
    if not hits and bounded_symbol_limit and include_fuzzy_symbols is not False:
        recovery_terms = query_terms(stripped)
        if recovery_terms:
            candidates = store.keyword_symbol_candidates(recovery_terms, include_body=bounded_text_limit > 0)
            candidates = _filter_rows_for_tests(candidates, include_tests=include_tests)
            recovered = rank_keyword_candidates(candidates, recovery_terms)[: min(bounded_limit, bounded_symbol_limit)]
            hits = [
                replace(
                    _hit_from_symbol(stripped, {**row, "summary": row["doc"]}, index=index), score=row["recovery_score"]
                )
                for index, row in enumerate(recovered)
            ]
            symbol_rows = recovered
    return LookupResult(
        query=stripped,
        repo_path=str(Path(repo_path).resolve()),
        symbols=len(symbol_rows),
        files=len(file_rows),
        text_matches=len(text_matches),
        hits=hits,
        recovery_terms=tuple(recovery_terms),
    )


def lookup_selected_paths(result: LookupResult) -> set[str]:
    """Return unique file paths selected by a lookup result.

    Args:
        result: Combined lookup result.

    Returns:
        Set of cataloged file paths represented in the ranked hits.
    """
    return {hit.path for hit in result.hits if hit.path}


def lookup_to_dict(result: LookupResult, *, store: CatalogStore | None = None) -> dict[str, Any]:
    """Serialize a lookup result to dictionaries.

    Args:
        result: Lookup result to serialize.
        store: Include indexed excerpts for the first three symbol hits when
            supplied, bounded to 40 lines and 6000 characters per excerpt.

    Returns:
        JSON-serializable lookup payload.
    """
    payload = {
        "query": result.query,
        "repo_path": result.repo_path,
        "symbols": result.symbols,
        "files": result.files,
        "text_matches": result.text_matches,
        "count": len(result.hits),
        "hits": [
            {
                "kind": hit.kind,
                "path": hit.path,
                "line": hit.line,
                "label": hit.label,
                "detail": hit.detail,
                "score": hit.score,
                "summary": hit.summary,
                "end_line": hit.end_line,
            }
            for hit in result.hits
        ],
    }
    if result.recovery_terms:
        payload["recovery"] = {
            "strategy": "bounded_keywords",
            "terms": list(result.recovery_terms),
            "max_candidates": 320,
        }
    if not result.hits:
        payload["guidance"] = QUERY_GUIDANCE
    if store is not None and store.supports_text_index():
        for row in payload["hits"][:3]:
            if row["kind"] != "symbol":
                continue
            start = max(1, row["line"] - 2)
            end = min(row["end_line"] or row["line"], start + 39)
            lines = store.source_line_range(row["path"], start, end)
            content = "\n".join(lines.get(number, "") for number in range(start, end + 1))
            row["context"] = {
                "start_line": start,
                "end_line": end,
                "content": content[:6000],
                "truncated": len(content) > 6000 or (row["end_line"] or end) > end,
            }
    return payload


def _rank_hits(
    *,
    query: str,
    symbol_rows: list[dict[str, Any]],
    file_rows: list[dict[str, Any]],
    text_matches: list[TextMatch],
    limit: int,
) -> list[LookupHit]:
    hits: list[LookupHit] = []
    seen: set[tuple[str, int, str]] = set()
    symbol_locations: set[tuple[str, int]] = set()

    for index, row in enumerate(symbol_rows):
        hit = _hit_from_symbol(query=query, row=row, index=index)
        key = (hit.path, hit.line, hit.kind)
        if key in seen:
            continue
        seen.add(key)
        symbol_locations.add((hit.path, hit.line))
        hits.append(hit)

    for index, row in enumerate(file_rows):
        hit = _hit_from_file(query=query, row=row, index=index)
        key = (hit.path, hit.line, hit.kind)
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)

    for index, match in enumerate(text_matches):
        hit = _hit_from_text(query, match, index=index)
        if (hit.path, hit.line) in symbol_locations:
            continue
        key = (hit.path, hit.line, hit.kind)
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)

    return sorted(hits, key=lambda hit: (hit.score, hit.path, hit.line, hit.kind))[:limit]


def _candidate_limit(*, limit: int, include_tests: bool) -> int:
    bounded_limit = max(1, min(limit, 100))
    if include_tests:
        return bounded_limit
    return min(100, max(bounded_limit, bounded_limit * 5 + 10))


def _search_symbol_rows(
    repo_path: str | Path,
    store: CatalogStore,
    query_variants: list[str],
    *,
    limit: int,
    include_fuzzy: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int]] = set()
    prefix_rows = [
        {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "path": row["path"],
            "line": row["line"],
            "end_line": row["end_line"],
            "signature": row["signature"],
            "summary": row["doc"],
            "provider": "catalog",
        }
        for row in store.search_symbol_prefixes(query_variants, limit=limit)
    ]
    _extend_symbol_rows(rows, seen, prefix_rows, limit=limit)
    if rows:
        return rows
    if not include_fuzzy:
        return rows
    if not _should_search_fuzzy_symbols(query_variants):
        return rows

    _extend_symbol_rows(
        rows,
        seen,
        search_symbols(
            repo_path,
            store,
            query_variants[0],
            limit=limit,
            provider="catalog",
            include_fuzzy=True,
        ).symbols,
        limit=limit,
    )
    return rows


def _should_search_fuzzy_symbols(query_variants: list[str]) -> bool:
    if len(query_variants) <= 1:
        return True
    original_query = query_variants[0].strip()
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:.]*", original_query))


def _extend_symbol_rows(
    rows: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str, int]],
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> None:
    for row in candidates:
        key = (
            str(row.get("path", "")),
            str(row.get("kind", "")),
            str(row.get("name", "")),
            str(row.get("qualified_name", "")),
            int(row.get("line", 0) or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            return


def _search_file_rows(store: CatalogStore, query_variants: list[str], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in store.search_files_many(query_variants, limit=limit):
        payload = dict(row)
        path = str(payload.get("path", ""))
        if path in seen:
            continue
        seen.add(path)
        rows.append(payload)
        if len(rows) >= limit:
            return rows
    return rows


def _filter_rows_for_tests(rows: list[dict[str, Any]], *, include_tests: bool) -> list[dict[str, Any]]:
    if include_tests:
        return rows
    return [row for row in rows if not _is_test_path(str(row.get("path", "")))]


def _filter_text_matches_for_tests(matches: list[TextMatch], *, include_tests: bool) -> list[TextMatch]:
    if include_tests:
        return matches
    return [match for match in matches if not _is_test_path(match.path)]


def _hit_from_symbol(query: str, row: dict[str, Any], *, index: int) -> LookupHit:
    name = str(row.get("name", ""))
    qualified_name = str(row.get("qualified_name", "")) or name
    signature = str(row.get("signature", ""))
    score = _symbol_score(query, name=name, qualified_name=qualified_name, index=index)
    if score >= 1000 and _is_test_path(str(row.get("path", ""))):
        score += 1500
    return LookupHit(
        kind="symbol",
        path=str(row.get("path", "")),
        line=int(row.get("line", 0) or 0),
        label=f"{row.get('kind', 'symbol')} {qualified_name}",
        detail=signature,
        score=score,
        end_line=int(row["end_line"]) if row.get("end_line") is not None else None,
        summary=str(row.get("summary", ""))[:500],
    )


def _hit_from_file(query: str, row: dict[str, Any], *, index: int) -> LookupHit:
    path = str(row.get("path", ""))
    language = str(row.get("language", ""))
    line_count = int(row.get("line_count", 0) or 0)
    score = _file_score(query, path=path, index=index)
    return LookupHit(
        kind="file",
        path=path,
        line=1,
        label=path,
        detail=f"{language}, {line_count} lines",
        score=score,
    )


def _hit_from_text(query: str, match: TextMatch, *, index: int) -> LookupHit:
    detail = match.snippet or match.content
    return LookupHit(
        kind="text",
        path=match.path,
        line=match.line,
        label=match.content.strip(),
        detail=detail,
        score=_text_score(query=query, match=match, index=index),
    )


def _text_score(query: str, match: TextMatch, *, index: int) -> int:
    score = TEXT_HIT_SCORE_BASE + index
    normalized_query = query.casefold()
    normalized_content = match.content.casefold()
    normalized_path = match.path.casefold()
    if normalized_query and normalized_query in normalized_content:
        score -= 20
        if _looks_like_ui_metadata_line(normalized_content):
            score -= 70
    if _looks_like_ui_route_path(normalized_path):
        score -= 180
    elif _looks_like_ui_page_path(normalized_path):
        score -= 130
    elif _looks_like_ui_config_path(normalized_path):
        score -= 80
    if _is_test_path(match.path):
        score += 300
    return max(TEXT_HIT_SCORE_FLOOR, score)


def _looks_like_ui_metadata_line(normalized_content: str) -> bool:
    return any(marker in normalized_content for marker in UI_METADATA_LINE_MARKERS)


def _looks_like_ui_route_path(normalized_path: str) -> bool:
    return any(marker in normalized_path for marker in UI_ROUTE_PATH_MARKERS)


def _looks_like_ui_page_path(normalized_path: str) -> bool:
    return any(marker in normalized_path for marker in UI_PAGE_PATH_MARKERS)


def _looks_like_ui_config_path(normalized_path: str) -> bool:
    return "/config/" in normalized_path or normalized_path.startswith("config/")


def _symbol_score(query: str, *, name: str, qualified_name: str, index: int) -> int:
    normalized = query.casefold()
    query_key = _identifier_key(query)
    name_key = _identifier_key(name)
    qualified_name_key = _identifier_key(qualified_name)
    if name.casefold() == normalized:
        return index
    if qualified_name.casefold() == normalized:
        return 100 + index
    if query_key and name_key == query_key:
        return 50 + index
    if query_key and qualified_name_key == query_key:
        return 150 + index
    if name.casefold().startswith(normalized):
        return 200 + index
    if qualified_name.casefold().startswith(normalized):
        return 300 + index
    if query_key and name_key.startswith(query_key):
        return 250 + index
    if query_key and qualified_name_key.startswith(query_key):
        return 350 + index
    return 1000 + index


def _file_score(query: str, *, path: str, index: int) -> int:
    normalized = query.casefold().lstrip("./")
    normalized_path = path.casefold()
    basename = Path(path).name.casefold()
    stem = Path(path).stem.casefold()
    query_key = _identifier_key(query)
    path_key = _identifier_key(path)
    basename_key = _identifier_key(Path(path).name)
    stem_key = _identifier_key(Path(path).stem)
    if normalized_path == normalized:
        return 400 + index
    if basename == normalized or stem == normalized:
        return 500 + index
    if query_key and (basename_key == query_key or stem_key == query_key):
        return 520 + index
    if basename.startswith(normalized) or stem.startswith(normalized):
        return 600 + index
    if query_key and (basename_key.startswith(query_key) or stem_key.startswith(query_key)):
        return 620 + index
    if query_key and path_key.startswith(query_key):
        return 640 + index
    if query_key and query_key in path_key:
        return 650 + index
    return 700 + index


def _query_variants(query: str) -> list[str]:
    variants = [query]
    tokens = _query_tokens(query)
    if len(tokens) < 2:
        return variants

    pascal = "".join(_title_token(token) for token in tokens)
    camel = tokens[0].casefold() + "".join(_title_token(token) for token in tokens[1:])
    snake = "_".join(token.casefold() for token in tokens)
    kebab = "-".join(token.casefold() for token in tokens)
    compact = "".join(token.casefold() for token in tokens)
    for variant in (pascal, camel, snake, kebab, compact):
        if variant and variant not in variants:
            variants.append(variant)
        if len(variants) >= MAX_LOOKUP_QUERY_VARIANTS:
            break
    return variants


def _query_tokens(query: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9]+", query) if token]


def _title_token(token: str) -> str:
    if not token:
        return ""
    return f"{token[0].upper()}{token[1:]}"


def _identifier_key(value: str) -> str:
    return "".join(_query_tokens(value)).casefold()


def _has_exact_symbol_match(query: str, symbol_rows: list[dict[str, Any]]) -> bool:
    normalized = query.casefold()
    return any(
        str(row.get("name", "")).casefold() == normalized or str(row.get("qualified_name", "")).casefold() == normalized
        for row in symbol_rows
    )


def _has_exact_file_match(query: str, file_rows: list[dict[str, Any]]) -> bool:
    normalized = query.casefold().lstrip("./")
    for row in file_rows:
        path = str(row.get("path", ""))
        if path.casefold() == normalized:
            return True
        parsed = Path(path)
        if parsed.name.casefold() == normalized or parsed.stem.casefold() == normalized:
            return True
    return False


def _is_test_path(path: str) -> bool:
    parsed = Path(path)
    return (
        "tests" in parsed.parts
        or parsed.name.startswith("test_")
        or ".test." in parsed.name
        or parsed.name.endswith("_test.py")
    )


def _default_text_limit(
    query: str,
    *,
    symbol_rows: list[dict[str, Any]],
    file_rows: list[dict[str, Any]],
    limit: int,
) -> int:
    if _has_exact_symbol_match(query, symbol_rows):
        return DEFAULT_EXACT_SYMBOL_TEXT_LIMIT
    if _has_exact_file_match(query, file_rows):
        return DEFAULT_EXACT_FILE_TEXT_LIMIT
    return limit
