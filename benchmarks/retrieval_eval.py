"""Deterministic public-source retrieval checks without model calls."""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from code_intel.agent_execution import run_private
from code_intel.catalog_store import CatalogStore
from code_intel.cataloger import build_catalog
from code_intel.integrations import private_json
from code_intel.lookup import lookup, lookup_to_dict

CASES = [
    (
        "function supplies missing integration configuration defaults enabled timeout_seconds schema_version",
        "migrate_config",
    ),
    ("scheduled index refresh interval launchd", "build_launchd_refresh_job"),
    ("atomically publishes a full replacement catalog", "publish_catalog"),
    (
        "atomically publishes a full replacement catalog preserves usage history write lock file replacement",
        "publish_catalog",
    ),
    ("write private JSON with owner permissions", "private_json"),
    ("terminate process group on timeout", "run_private"),
    ("normalize raw provider usage cache reads", "normalize_usage"),
    ("resolve named workspace repositories", "resolve_workspace_repos"),
]


def evaluate(repo: Path, revision: str) -> dict:
    """Compare exact/phrase search with recovery using eight fixed source questions."""
    records = []
    with tempfile.TemporaryDirectory(prefix="code-intel-retrieval-") as directory:
        root = Path(directory) / "repo"
        for command in (
            ["git", "clone", "--quiet", "--no-hardlinks", "--", str(repo.resolve()), str(root)],
            ["git", "-C", str(root), "checkout", "--quiet", "--detach", revision],
        ):
            if run_private(command, cwd=Path(directory)).returncode:
                raise RuntimeError("Could not prepare fixed source revision")
        build_catalog(root)
        store = CatalogStore.for_repo(root)
        for query, expected in CASES:
            for mode in ("exact_phrase", "keyword_recovery"):
                started = time.perf_counter()
                result = lookup(
                    root,
                    store,
                    query,
                    limit=3,
                    include_tests=False,
                    include_fuzzy_symbols=False if mode == "exact_phrase" else None,
                )
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                labels = [hit.label.rsplit(".", 1)[-1].removeprefix("function ") for hit in result.hits]
                records.append(
                    {
                        "query": query,
                        "expected_symbol": expected,
                        "mode": mode,
                        "hit_at_3": expected in labels,
                        "elapsed_ms": elapsed,
                        "hits": lookup_to_dict(result)["hits"],
                    }
                )
    summary = {
        mode: {"queries": len(CASES), "hits_at_3": sum(row["hit_at_3"] for row in records if row["mode"] == mode)}
        for mode in ("exact_phrase", "keyword_recovery")
    }
    return {
        "source_revision": revision,
        "summary": summary,
        "records": records,
        "limitation": "Small crafted regression set; not a blinded or representative retrieval benchmark",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default="f675a1ddc78614ca4131ffc38396b55e4cf615ba")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.repo, args.revision)
    private_json(args.output, report)
    print(report["summary"])
