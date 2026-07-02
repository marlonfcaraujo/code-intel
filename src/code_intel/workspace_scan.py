"""Workspace-wide catalog refresh helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from code_intel.cataloger import build_catalog

DEFAULT_WORKSPACE_SCAN_WORKERS = 4
MAX_WORKSPACE_SCAN_WORKERS = 16


@dataclass(frozen=True, slots=True)
class WorkspaceScanRepoResult:
    """Catalog refresh result for one repository.

    Attributes:
        repo_path: Absolute repository path requested for scanning.
        ok: Whether the scan completed successfully.
        catalog: Catalog result payload from ``build_catalog`` when the scan
            succeeds.
        error: Error text when the scan fails.
    """

    repo_path: str
    ok: bool
    catalog: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceScanReport:
    """Catalog refresh report for a multi-repository workspace.

    Attributes:
        repo_count: Number of repositories requested.
        scanned_count: Number of repositories refreshed successfully.
        failed_count: Number of repositories that failed to refresh.
        incremental: Whether scans requested incremental catalog reuse.
        workers: Per-repository analysis worker override, if supplied.
        repo_workers: Number of repositories scanned concurrently.
        total_ms: End-to-end elapsed milliseconds for the workspace scan.
        repos: Per-repository refresh results in requested order.
    """

    repo_count: int
    scanned_count: int
    failed_count: int
    incremental: bool
    workers: int | None
    repo_workers: int
    total_ms: float
    repos: list[WorkspaceScanRepoResult]


def scan_workspace(
    repos: list[str | Path],
    *,
    incremental: bool = False,
    workers: int | None = None,
    repo_workers: int | None = None,
    skip_unchanged_meta: bool = False,
) -> WorkspaceScanReport:
    """Refresh catalogs for several repositories in one process.

    Args:
        repos: Repository roots to catalog.
        incremental: Whether each repository should reuse unchanged analyses
            when possible.
        workers: Optional per-repository analysis worker override.
        repo_workers: Optional number of repositories to refresh concurrently.
        skip_unchanged_meta: Whether no-change incremental scans should skip
            metadata-only SQLite writes.

    Returns:
        Workspace scan report with per-repository results and timing.

    Raises:
        ValueError: If no repositories are supplied.
    """
    repo_paths = _resolve_repo_paths(repos)
    effective_repo_workers = _effective_repo_workers(repo_workers, repo_count=len(repo_paths))
    started = perf_counter()
    if effective_repo_workers <= 1 or len(repo_paths) <= 1:
        results = [
            _scan_one_repo(
                repo_path,
                incremental=incremental,
                workers=workers,
                skip_unchanged_meta=skip_unchanged_meta,
            )
            for repo_path in repo_paths
        ]
    else:
        with ThreadPoolExecutor(max_workers=effective_repo_workers) as executor:
            results = list(
                executor.map(
                    lambda repo_path: _scan_one_repo(
                        repo_path,
                        incremental=incremental,
                        workers=workers,
                        skip_unchanged_meta=skip_unchanged_meta,
                    ),
                    repo_paths,
                )
            )

    scanned_count = sum(1 for result in results if result.ok)
    failed_count = len(results) - scanned_count
    return WorkspaceScanReport(
        repo_count=len(repo_paths),
        scanned_count=scanned_count,
        failed_count=failed_count,
        incremental=incremental,
        workers=workers,
        repo_workers=effective_repo_workers,
        total_ms=_elapsed_ms(started),
        repos=results,
    )


def workspace_scan_to_dict(report: WorkspaceScanReport) -> dict[str, Any]:
    """Serialize a workspace scan report.

    Args:
        report: Workspace scan report.

    Returns:
        JSON-serializable dictionary.
    """
    return asdict(report)


def _scan_one_repo(
    repo_path: Path,
    *,
    incremental: bool,
    workers: int | None,
    skip_unchanged_meta: bool,
) -> WorkspaceScanRepoResult:
    try:
        result = build_catalog(
            repo_path,
            incremental=incremental,
            workers=workers,
            skip_unchanged_meta=skip_unchanged_meta,
        )
    except Exception as exc:
        return WorkspaceScanRepoResult(
            repo_path=str(repo_path),
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    return WorkspaceScanRepoResult(repo_path=str(repo_path), ok=True, catalog=asdict(result))


def _resolve_repo_paths(repos: list[str | Path]) -> list[Path]:
    if not repos:
        raise ValueError("at least one repository path is required")
    return [Path(repo).expanduser().resolve() for repo in repos]


def _effective_repo_workers(repo_workers: int | None, *, repo_count: int) -> int:
    if repo_workers is None or repo_workers <= 0:
        return min(DEFAULT_WORKSPACE_SCAN_WORKERS, max(1, repo_count))
    return min(MAX_WORKSPACE_SCAN_WORKERS, max(1, repo_workers), max(1, repo_count))


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)
