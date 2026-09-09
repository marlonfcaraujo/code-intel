# Measured Codex pilot

On September 9, 2026, a local pilot measured actual Codex CLI usage against the
public code-intel repository at commit
`f675a1ddc78614ca4131ffc38396b55e4cf615ba`. It requested `gpt-5.6-luna` with low
reasoning effort using Codex CLI 0.149.1 and existing ChatGPT authentication.

**This is a measurement example, not evidence of causal or billed savings.**

## Results

Three source-navigation tasks were repeated twice per arm, giving six paired
comparisons and twelve agent runs. All answers passed deterministic checks.

| Metric, summed across six runs per arm | Basic read/search tools | Same tools plus code-intel |
| --- | ---: | ---: |
| Successful tasks | 6/6 | 6/6 |
| Input tokens, including cached input | 488,746 | 390,743 |
| Cached input tokens, already included above | 337,664 | 238,336 |
| Uncached input tokens, derived by subtraction | 151,082 | 152,407 |
| Output tokens | 2,354 | 2,169 |
| Reported cache-write tokens | 0 | 0 |
| Agent elapsed time, excluding preparation/indexing | 107.305 s | 101.241 s |

The reduction in summed input was approximately 20.05%; the median per-pair
input reduction was 9.25%. Those are different statistics. Uncached input was
approximately 0.88% higher with code-intel offered. Provider cache conditions
were uncontrolled, and there is no invoice or price-based cost calculation.

Fresh indexing for the six treatment runs added 4.333 seconds in total and is
reported separately. Index reuse would change that overhead. API-call counts
remain unknown: a Codex turn usage total can cover several model requests.

## Why these results do not establish a code-intel advantage

The model attempted the specialized lookup tool in four of six treatment runs.
All four calls used long natural-language phrases and returned zero matches.
The context-pack tool was never called. All successful answers ultimately came
from the basic read/search tools, which both arms had.

This means the observed differences cannot be attributed to useful code-intel
retrieval. Tool selection and planning varied between runs, and two treatment
runs never attempted a specialized tool at all. Publishing only the lower total
input number would hide this important limitation.

The next experiment should separately test concise identifier/substring guidance
and verify that specialized retrieval returns useful context. It should retain
this pilot as the original result rather than replace it with a favorable run.
More tasks, larger public repositories and repetitions would be needed before
making a general savings claim.

## Method

- Tasks locate integration defaults, the refresh schedule, and atomic catalog
  publication/history preservation in source code.
- Each arm starts from a fresh disposable checkout of the same commit.
- Both arms expose identical source-file listing, regex search and bounded read
  tools through one local MCP server. The treatment additionally offers ranked
  lookup and context packs backed by code-intel.
- Reads are confined to Python files under `src/`. Parent traversal and symlink
  escapes are rejected. Tests and grader code are not exposed to the model.
- Shell, web, plugins, memory, multi-agent execution and project instructions are
  disabled. The Codex dispatcher remains enabled because it routes MCP calls.
- Every completed tool-call trace was checked against the per-arm allowlist.
  This validates observed calls; it is separate from the generic report's
  `tool_policy_independently_verified` field, which remains false.
- The same prompt and reasoning settings are used in both arms. Arm order
  alternates across tasks and repetitions. No task result was excluded from the
  twelve-run measurement series. Earlier setup smoke attempts are excluded.
- The grader compares final JSON answers against explicit expected values and
  types, including booleans and numeric defaults. Model self-assessment is not
  used as the success criterion.
- Token values come from terminal Codex usage events. Cached input is not added
  to total input a second time. Missing counters remain unknown.
- Successful tool-result JSON byte sizes in detailed receipts are measured
  before MCP serialization; they are not model output-token counts.

## Reproduce

Install Codex and authenticate normally. In a checkout of this repository with
the harness available:

```bash
uv sync --extra mcp --group dev --frozen
uv run python benchmarks/codex_pilot.py run \
  --repo /path/to/pinned-source-checkout \
  --output .code-intel/experiments/my-pilot \
  --model gpt-5.6-luna --repeat 2
```

The target checkout's HEAD is pinned for all runs. Use the commit above to repeat
the same source tasks. Run this command only when you intend to consume model
usage. It makes twelve agent runs; each has a 180-second timeout by default.
Output directories must be new to prevent overwriting prior receipts.

Raw transcripts stay in the ignored local output directory with owner-only
permissions. The published [aggregate and per-run counters](benchmarks/codex-pilot-2026-09-09.json)
contain only public task labels, token counts, tool names and timings. No private
repositories, prompts, credentials, session IDs or machine paths are included.
