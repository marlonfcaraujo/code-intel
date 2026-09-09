# Function BM25 and larger public tasks

Version 0.4 adds one SQLite FTS5 table, not a new service. Exact name/path lookup
remains first. Other multiword queries rank symbol documents using name,
signature, docstring and body weights of 8, 3, 5 and 1. The tokenizer is Porter
over Unicode words, with snake/camel/acronym boundaries added for code identifiers.
The existing trigram index remains the substring-search path.

Documents include at most 2,000 docstring characters and 120 body lines / 8,000
body characters. Class bodies are excluded to avoid duplicating every method;
their names and docstrings remain searchable. Queries cap terms at eight and
candidates at 64, require distinct term overlap, and filter test paths before
candidate truncation when tests are excluded. This is lexical ranking, not
semantic understanding. Content beyond the body budget may need literal search.

Documents are inserted/deleted with existing catalog transactions. Unchanged
scans do not rewrite them. Context packs already merge overlapping windows;
no second deduplication layer or custom query cache was introduced.

## Index overhead

The fixture is pytest 8.4.2 at
`bfae4224fd554d3d7f2c277a4cc092b6ec6af3ae`: 261 indexed Python files and 5,730
symbols, including test files in the catalog. The profile alternated code-intel
0.3.0 (`c833521333bcdd7deaf23df1da3e75945879953a`) and the new implementation
twice on one machine, using the same fixture and separate databases. OS caches
were not cleared. Timings are in-process after imports, not whole CLI startup.

| Median observed metric | 0.3.0 | Function BM25 |
| --- | ---: | ---: |
| Full index build | 2.512 s | 2.979 s |
| Unchanged incremental scan | 32.2 ms | 45.6 ms |
| Database bytes | 31,170,560 | 38,473,728 |
| Files rewritten by unchanged scan | 0 | 0 |
| `FixtureDef.finish` lookup | 1.465 ms | 1.506 ms |
| `Config.parse` lookup | 2.054 ms | 1.862 ms |
| `pytest_runtestloop` lookup | 3.257 ms | 3.413 ms |
| `fixture cached result finalizers` | 59.611 ms | 7.108 ms |
| `test setup teardown reports` | 127.094 ms | 12.564 ms |
| `command line configuration session` | 53.688 ms | 6.490 ms |

Full indexing took about 18.6% longer and the database grew about 23.4% in this
sample. Broader indexed lookups were substantially faster; exact lookups stayed
similar. These few queries and repetitions are not a general latency guarantee.
Ranking outputs are retained in the [profile data](benchmarks/bm25-index-profile-2026-09-09.json).

## Agent pilot

Three new tasks trace CLI/session dispatch, per-item execution/reporting, and
fixture teardown across source files. Each task ran twice per arm, alternating
order, using Codex CLI 0.149.1 with requested model `gpt-5.6-luna`, low reasoning.
The same restricted source tools, fresh checkouts, deterministic graders and
trace checks as the [earlier pilots](BENCHMARKS.md) were used. The baseline has
basic source read/search tools; treatment adds the updated code-intel tools.
Only `src/` Python files are exposed to the model.

| Sum across six runs per arm | Baseline | Code-intel available |
| --- | ---: | ---: |
| Tasks passed | 6/6 | 6/6 |
| Input tokens, including cached | 583,636 | 475,283 |
| Cached input, included above | 431,872 | 312,576 |
| Uncached input | 151,764 | 162,707 |
| Output tokens | 3,556 | 2,800 |
| Reported cache-write tokens | 0 | 0 |
| Agent elapsed time, excluding preparation/indexing | 138.972 s | 108.028 s |

Total input fell 18.6%, output 21.3%, and agent time 22.3%; **uncached input rose
7.2%**. Median paired input reduction was 19.7% and median paired time reduction
20.8%. All results are retained. All 27 specialized calls returned matches:
12 used function BM25 and 15 used exact/phrase lookup. No context-pack calls were
made. Non-empty results alone are not proof of relevance.

The six fresh treatment indexes added **25.079 seconds**. Agent time plus that
indexing is 133.107 seconds versus baseline agent time of 138.972 seconds, only
about 4.2% lower. This combined comparison still excludes cloning and other
preparation. Day-to-day index reuse amortizes initial indexing, but that reused
end-to-end workflow was not measured by these fresh-checkout trials.

This is not a pure BM25 ablation: the agent baseline lacks code-intel entirely.
The separate index profile compares 0.3.0 with the new implementation. Cache
conditions were uncontrolled, API-call counts are unknown, and no invoice-based
cost comparison is made. A larger task did not guarantee lower uncached input.
See the [per-run counters](benchmarks/pytest-bm25-2026-09-09.json).

## Reproduce

Clone the public fixture and install the existing optional MCP dependency:

```bash
git clone --depth 1 --branch 8.4.2 https://github.com/pytest-dev/pytest.git /path/to/pytest
uv sync --extra mcp --group dev --frozen
uv run python benchmarks/index_profile.py --fixture /path/to/pytest \
  --output .code-intel/experiments/index-profile.json
```

The index profile makes no model calls. The following command consumes model
usage and makes twelve agent runs; choose a new output directory:

```bash
uv run python benchmarks/codex_pilot.py run --suite pytest --repo /path/to/pytest \
  --revision bfae4224fd554d3d7f2c277a4cc092b6ec6af3ae \
  --output .code-intel/experiments/pytest-pilot --repeat 2
```

Raw transcripts remain local and ignored. Published artifacts contain public
repository identifiers and numerical results, not private repository data.
