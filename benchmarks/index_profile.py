"""Compare package indexing overhead on a fixed public fixture, without model calls."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median

from code_intel.integrations import private_json

PROBE = """
import json,sys,time
from pathlib import Path
from code_intel.cataloger import build_catalog,CATALOG_ANALYZER_VERSION
from code_intel.catalog_store import CatalogStore
from code_intel.lookup import lookup
repo,db=Path(sys.argv[1]),Path(sys.argv[2])
start=time.perf_counter(); result=build_catalog(repo,db); full=(time.perf_counter()-start)*1000
start=time.perf_counter(); incremental=build_catalog(repo,db,incremental=True,skip_unchanged_meta=True)
incremental_ms=(time.perf_counter()-start)*1000
store=CatalogStore(db,reuse_connection=True)
queries=[]
for query in ('FixtureDef.finish','Config.parse','pytest_runtestloop',
              'fixture cached result finalizers','test setup teardown reports','command line configuration session'):
    times=[]
    for _ in range(3):
        start=time.perf_counter(); hits=lookup(repo,store,query,limit=3,include_tests=False)
        times.append(round((time.perf_counter()-start)*1000,3))
    queries.append({'query':query,'times_ms':times,'top_labels':[hit.label for hit in hits.hits]})
store.close()
print(json.dumps({'analyzer':CATALOG_ANALYZER_VERSION,'files':result.file_count,'symbols':result.symbol_count,
                  'full_scan_ms':round(full,3),'unchanged_scan_ms':round(incremental_ms,3),
                  'unchanged_written_files':incremental.written_file_count,'database_bytes':db.stat().st_size,
                  'queries':queries}))
"""


def main() -> None:
    """Alternate old and new implementations against the same fixture source."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).parents[1]
    records = []
    with tempfile.TemporaryDirectory(prefix="code-intel-profile-") as directory:
        root = Path(directory)
        baseline = root / "baseline"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(project), str(baseline)], check=True)
        subprocess.run(
            ["git", "-C", str(baseline), "checkout", "-q", "c833521333bcdd7deaf23df1da3e75945879953a"], check=True
        )
        for iteration in range(2):
            order = ("baseline", "bm25") if iteration == 0 else ("bm25", "baseline")
            for label in order:
                source = baseline if label == "baseline" else project
                env = {**os.environ, "PYTHONPATH": str(source / "src")}
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        PROBE,
                        str(args.fixture.resolve()),
                        str(root / f"{label}-{iteration}.sqlite"),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=True,
                )
                records.append({"implementation": label, "iteration": iteration, **json.loads(result.stdout)})
    summary = {}
    for label in ("baseline", "bm25"):
        selected = [row for row in records if row["implementation"] == label]
        summary[label] = {
            key: median(row[key] for row in selected) for key in ("full_scan_ms", "unchanged_scan_ms", "database_bytes")
        }
    report = {
        "fixture": "pytest 8.4.2",
        "source_revision": "bfae4224fd554d3d7f2c277a4cc092b6ec6af3ae",
        "baseline_revision": "c833521333bcdd7deaf23df1da3e75945879953a",
        "records": records,
        "summary": summary,
        "limitations": "Two alternating repetitions on one machine; OS caches not cleared; no model calls",
    }
    private_json(args.output, report)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
