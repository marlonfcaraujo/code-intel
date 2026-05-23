"""Change reports for cataloged repositories."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from code_intel.catalog_store import CatalogStore
from code_intel.models import ChangeReport
from code_intel.tests_map import find_related_tests


def compute_change_report(repo_root: Path, store: CatalogStore, file_path: str) -> ChangeReport:
    """Compute a dependency-aware change report for ``file_path``."""
    resolved_path = store.resolve_file_path(file_path)
    if resolved_path is None:
        raise ValueError(f"File is not cataloged: {file_path}")

    reverse_graph = _build_reverse_graph(store)
    direct_dependents = sorted(reverse_graph.get(resolved_path, set()))
    transitive_dependents = _transitive_dependents(reverse_graph, resolved_path, direct_dependents)
    related_tests = find_related_tests(repo_root, store, resolved_path)
    blast_score = round((len(direct_dependents) * 2) + len(transitive_dependents) + (len(related_tests) * 0.5), 2)

    return ChangeReport(
        path=resolved_path,
        direct_dependents=direct_dependents,
        transitive_dependents=transitive_dependents,
        related_tests=related_tests,
        blast_score=blast_score,
        risk=_risk_label(blast_score),
    )


def _build_reverse_graph(store: CatalogStore) -> dict[str, set[str]]:
    reverse_graph: dict[str, set[str]] = defaultdict(set)
    for dependency in store.list_internal_dependencies():
        reverse_graph[str(dependency["target_path"])].add(str(dependency["source_path"]))
    return reverse_graph


def _transitive_dependents(
    reverse_graph: dict[str, set[str]],
    target_path: str,
    direct_dependents: list[str],
) -> list[str]:
    visited = set(direct_dependents)
    queue: deque[str] = deque(direct_dependents)

    while queue:
        current = queue.popleft()
        for parent in reverse_graph.get(current, set()):
            if parent != target_path and parent not in visited:
                visited.add(parent)
                queue.append(parent)

    return sorted(visited - set(direct_dependents))


def _risk_label(blast_score: float) -> str:
    if blast_score >= 30:
        return "critical"
    if blast_score >= 15:
        return "high"
    if blast_score >= 5:
        return "medium"
    return "low"
