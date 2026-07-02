"""Estimated savings calculations for code-intel lookups."""

from __future__ import annotations

from pathlib import Path

from code_intel.catalog_store import CatalogStore

TOKENS_PER_LINE_ESTIMATE = 8
TOKENS_PER_RESULT_ESTIMATE = 40


def estimate_saved_tokens_for_paths(store: CatalogStore, selected_paths: set[str], result_count: int) -> dict[str, int]:
    """Estimate tokens saved by using targeted catalog results instead of broad file reads."""
    if not store.has_catalog():
        return {
            "candidate_files": 0,
            "returned_files": len(selected_paths),
            "estimated_saved_tokens": 0,
        }

    token_summary = store.file_token_summary(
        selected_paths=selected_paths,
        tokens_per_line=TOKENS_PER_LINE_ESTIMATE,
    )
    total_tokens = token_summary["total_tokens"]
    selected_tokens = token_summary["selected_tokens"]
    result_tokens = max(0, result_count) * TOKENS_PER_RESULT_ESTIMATE
    saved = max(0, total_tokens - selected_tokens - result_tokens)
    return {
        "candidate_files": token_summary["file_count"],
        "returned_files": len(selected_paths),
        "estimated_saved_tokens": saved,
    }


def estimate_saved_tokens_for_context_pack(
    store: CatalogStore,
    selected_paths: set[str],
    returned_tokens: int,
    result_count: int,
) -> dict[str, int]:
    """Estimate tokens saved by returning bounded source snippets.

    Args:
        store: Catalog store for the searched repository.
        selected_paths: Cataloged files represented by returned snippets.
        returned_tokens: Estimated tokens for the snippet content.
        result_count: Number of returned snippets or ranked hit records.

    Returns:
        Metrics compatible with ``CatalogStore.record_usage_event``.
    """
    if not store.has_catalog():
        return {
            "candidate_files": 0,
            "returned_files": len(selected_paths),
            "estimated_saved_tokens": 0,
        }

    token_summary = store.file_token_summary(tokens_per_line=TOKENS_PER_LINE_ESTIMATE)
    total_tokens = token_summary["total_tokens"]
    payload_tokens = max(0, returned_tokens) + max(0, result_count) * TOKENS_PER_RESULT_ESTIMATE
    saved = max(0, total_tokens - payload_tokens)
    return {
        "candidate_files": token_summary["file_count"],
        "returned_files": len(selected_paths),
        "estimated_saved_tokens": saved,
    }


def selected_paths_from_change_report(report: object) -> set[str]:
    """Return files touched by a change report object."""
    path = getattr(report, "path", "")
    direct = set(getattr(report, "direct_dependents", []))
    transitive = set(getattr(report, "transitive_dependents", []))
    related_tests = {match.path for match in getattr(report, "related_tests", [])}
    return {p for p in {path, *direct, *transitive, *related_tests} if p}


def selected_paths_from_rows(rows: list[dict]) -> set[str]:
    """Return unique paths from serialized search rows."""
    return {str(row["path"]) for row in rows if row.get("path")}


def selected_paths_from_test_matches(target_path: str, matches: list[object]) -> set[str]:
    """Return target and related test paths."""
    return {target_path, *(match.path for match in matches)}


def selected_paths_from_text_matches(matches: list[object]) -> set[str]:
    """Return unique paths from text match objects."""
    return {str(getattr(match, "path", "")) for match in matches if getattr(match, "path", "")}


def normalize_repo_path(repo_path: str | Path) -> Path:
    """Resolve a repository path for metrics calls."""
    return Path(repo_path).resolve()
