"""Risk scoring for indexed files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_intel.impact import compute_impact
from code_intel.storage import IndexStore
from code_intel.tests_map import TEST_PATH_PARTS


@dataclass(frozen=True, slots=True)
class RiskRow:
    """Risk summary for one file."""

    path: str
    score: float
    risk: str
    direct_dependents: int
    transitive_dependents: int
    related_tests: int
    line_count: int


def top_risk_files(repo_root: Path, store: IndexStore, limit: int = 20) -> list[RiskRow]:
    """Return the highest-risk source files."""
    rows: list[RiskRow] = []
    for file_row in store.list_files():
        path = str(file_row["path"])
        if _is_test_path(path):
            continue
        report = compute_impact(repo_root, store, path)
        rows.append(
            RiskRow(
                path=path,
                score=report.blast_score,
                risk=report.risk,
                direct_dependents=len(report.direct_dependents),
                transitive_dependents=len(report.transitive_dependents),
                related_tests=len(report.related_tests),
                line_count=int(file_row["line_count"]),
            )
        )
    rows.sort(key=lambda row: (row.score, row.line_count), reverse=True)
    return rows[:limit]


def _is_test_path(path: str) -> bool:
    parts = set(Path(path).parts)
    name = Path(path).name
    return bool(parts & TEST_PATH_PARTS) or name.startswith("test_") or ".test." in name or ".spec." in name
