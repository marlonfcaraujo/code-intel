# Measured usage

`measured-report` accepts one completed task per JSONL line. This synthetic
example describes one arm; supply a second record with the same controls and
pair ID and `arm: "code_intel"`:

```json
{"pair_id":"task-1-repeat-1","arm":"baseline","model":"example-model","revision":"example-revision","prompt_id":"task-1","config_id":"config-1","success":true,"elapsed_ms":1000,"calls":[{"provider":"openai","usage":{"input_tokens":100,"output_tokens":20,"input_tokens_details":{"cached_tokens":40}}}]}
```

Include every model call in the task, including retries with known usage. Missing
usage must be represented by an empty usage object, not by dropping the call.
Supported usage shapes are OpenAI Responses/Chat Completions and Anthropic
Messages. OpenAI input already includes cached tokens. Anthropic input totals
are the sum of input, cache-read input and cache-creation input; if any component
is missing, the normalized total is unknown. Raw provider records stay in your
private input file; the report exports only fixed labels and aggregate numbers.

Pairs require identical model, repository revision, prompt identifier and
configuration identifier. These identifiers are assertions supplied by the
adapter, not independent verification. Duplicate arms and incomplete pairs fail
closed. Failed tasks with valid usage remain in totals and success counts.
Percent reductions use the median of per-pair percentages, omitting unknown or
zero baselines and reporting the number of eligible pairs.

## Running experiments

A local manifest selects an executable argument array and task set:

```json
{
  "adapter": ["/absolute/path/to/agent-adapter"],
  "controls": {
    "model": "example-model",
    "revision": "example-revision",
    "prompt_id": "suite-version-1",
    "config_id": "same-budget-and-settings"
  },
  "tasks": [{"id": "task-1", "prompt": "Find the parser entry point"}]
}
```

The runner sends `{task, arm, repetition, controls}` as JSON on stdin. The adapter
returns a task record on stdout with matching controls, `success`, and `calls`.
The runner assigns pair IDs, arms, and measured elapsed milliseconds. Arm order
alternates across repetitions and tasks. Executables are invoked without a shell.
Adapter failures or timeouts stop the experiment; no partial comparison is emitted.

The adapter must create a fresh isolated session/check-out for each arm, expose
ordinary search/read tools to baseline and additionally code-intel to the other
arm, and evaluate success using the same independent checks. It is responsible
for provider credentials, spending limits, capturing usage and tool restrictions.
Only run trusted adapter executables. Built-in native adapters are available as
described below. Executing them uses the selected CLI's authentication and can
consume paid model usage; registration and diagnostics make no model calls.

Keep the same task/model/settings/revision in both arms. Record cache conditions
in your private experiment metadata; fresh sessions do not guarantee cold provider
caches. Include indexing time separately when interpreting end-to-end results.
Cost conversion, indexing amortization, tool-level payload telemetry and cache
condition enforcement are not implemented in these commands.

## Public reports

Keep manifests, raw responses, prompts, source paths and task-level logs outside
the checkout or under ignored `.code-intel/`. The report excludes all caller
strings and raw payloads. Numeric aggregates can still be confidential: review
them before publishing. Use synthetic fixtures in tests and examples. Never
commit raw experiment records merely because the report itself is anonymous.

## Native integrations

Register adapters in this order, using existing CLI installations:

```bash
uv run code-intel integrations add codex
uv run code-intel integrations add hermes
uv run code-intel integrations add paseo
uv run code-intel integrations add opencode
uv run code-intel integrations add claude
uv run code-intel integrations list
uv run code-intel integrations doctor
```

Registration lives under `~/.code-intel/integrations/` with owner-only file
permissions. It stores an executable path, enablement and a task timeout, never
credentials, transcripts or model configuration. Repeating the same registration
is idempotent and preserves custom fields. Run `upgrade-config` after package
updates to persist new defaults with backups. Use
`integrations --config-dir /private/path add codex --executable /path/to/codex`
for a separate installation. This registers usage adapters; it does not modify
the agent's global MCP configuration. Authentication is reused at execution time
and is explicitly shown as `not_checked` by diagnostics.

| Adapter | Usage source | Accounting |
| --- | --- | --- |
| Codex | `exec --json`, terminal `turn.completed.usage` | Turn totals, cached input included; API-call count unknown |
| Hermes | `-z --usage-file` JSON | Input excludes cache; add cache reads/writes; use reported `api_calls` |
| Paseo | CLI `inspect.LastUsage` | Registration only; rejected as a complete-task measurement |
| OpenCode | `run --format json`, `step_finish.part.tokens` | Sum complete steps; input excludes cache reads/writes |
| Claude Code | `--print --output-format stream-json`, terminal `result.usage` | Task totals; do not add assistant-message usage again |

Paseo launches provider agents. Its CLI `run` result has no usage and
`inspect.LastUsage` is a latest snapshot with missing counters converted to zero.
Import the underlying provider's native output for a measured comparison.
Paseo registration deliberately reports `task_usage_supported: false`; execution
and task import are blocked until a complete accounting source is available.

All counters here are **agent-reported usage**, not independently reconciled
invoices. A CLI may normalize missing upstream usage to zero before emitting it;
this package cannot recover that lost distinction. Parser-level missing fields
remain unknown. Recheck adapters when upgrading CLIs; the synthetic fixtures
cover the documented event shapes, not every provider/version combination.

### Import an existing run

```bash
uv run code-intel integrations import codex \
  --input /private/path/native-events.jsonl \
  --metadata /private/path/task-metadata.json \
  --output /private/path/task-record.jsonl
```

Metadata contains `pair_id`, `arm`, `model`, `revision`, `prompt_id`, `config_id`,
`elapsed_ms`, and an independently evaluated `success` boolean. Raw answer text
and extra metadata fields are dropped. Output is a new owner-readable JSONL file;
existing output files are never overwritten. Combine the two arms' private
records for `measured-report`. For Hermes use its usage file as input.

### Execute a native paired experiment

Set `adapter` to a registered name instead of an executable argument array:

```json
{
  "adapter": "codex",
  "controls": {
    "model": "explicit-model-id",
    "revision": "exact-local-commit-hash",
    "prompt_id": "suite-version-1",
    "config_id": "same-budget-and-settings"
  },
  "tasks": [{
    "id": "parser-task",
    "repo": "/private/path/repository",
    "prompt": "Identify the parser entry point.",
    "prepare": ["/private/path/prepare-experiment"],
    "verify": ["/private/path/grade-parser-answer"]
  }]
}
```

Run using `code-intel measured-run /private/path/manifest.json`. Each task gets
a disposable clone checked out at the exact commit. Uncommitted source changes
are excluded. The same preparation command runs for both arms and receives
`{task, arm, repetition, controls, timeout, checkout}` on stdin. It must configure
the agent's permitted tools and external isolation, returning
`{"isolated": true, "args": [], "env": {}}` with any required native CLI arguments
or environment overrides. It should provision ordinary tools for baseline and
add code-intel only for the treatment arm. Registering an adapter cannot define
the task's correct answer or its permitted tool set, so these experiment-specific
preparation and grading commands are still required.

**A fresh checkout is not an OS security sandbox.** Hermes one-shot mode
automatically bypasses approvals; OpenCode can load installed customizations.
Run experiments with those clients inside an appropriate external sandbox and
ensure preparation restricts tool access and side effects. Native defaults do
not prove baseline/treatment tool isolation. The preparation assertion is
caller-supplied and native records mark `tool_policy_verified: false`.

The grader receives `{answer, completed}` on stdin in the disposable checkout.
Exit 0 means the independent success check passed; exit 1 means task failure;
other exit codes indicate broken grading infrastructure and stop the experiment.
Native process completion alone is never counted as task success. Process
timeouts terminate the process group on POSIX. Preparation, agent and grading
each have a timeout; the reported paired-run elapsed time includes setup and
grading. No raw agent output is printed on errors.

### Interface references

- [Codex non-interactive output](https://developers.openai.com/codex/noninteractive/)
- [Hermes source](https://github.com/NousResearch/hermes-agent)
- [Paseo CLI](https://paseo.sh/docs/cli)
- [OpenCode v1.1.60 usage normalization](https://github.com/anomalyco/opencode/blob/v1.1.60/packages/opencode/src/session/index.ts)
- [Claude Code CLI](https://code.claude.com/docs/en/cli-reference)
