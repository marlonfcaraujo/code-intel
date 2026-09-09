# Updating code-intel

Update the code-intel installation once. Repository catalogs and local user
configuration are separate from the package and should not be deleted during
an update. The commands below assume a clean source checkout already tracking
the desired branch.

```bash
git pull --ff-only
uv sync --extra mcp --group dev --frozen
uv run code-intel --version
uv run code-intel upgrade-config --dry-run
uv run code-intel upgrade-config
uv run code-intel integrations doctor
```

If installed separately as a command, reinstall from the updated source checkout:

```bash
uv tool install --force '/path/to/code-intel[mcp]'
```

Restart the MCP connection in your agent to load the new implementation. Existing
processes continue running their loaded code until restarted. A refresh job that
uses `uv run --project /path/to/code-intel` picks up the updated checkout on its
next invocation; its registration does not need to be recreated for every update.

## Configuration compatibility

`upgrade-config` scans only JSON files in the `workspaces`, `benchmark-suites`,
and `integrations` directories under `~/.code-intel`. Use `--config-dir` to select
another root. Measurement records, benchmark history, credentials and agent
configuration files are outside this operation.

- Legacy files without `schema_version` are treated as schema 0.
- Workspaces and benchmark suites upgrade to schema 1.
- Integrations upgrade to schema 2, adding `enabled: true` and
  `timeout_seconds: 300` only when absent. Explicit values are preserved.
- Reads supply those defaults in memory without silently rewriting the file.
- Workspace/suite saves preserve unknown fields. Explicitly saved known fields
  still replace their prior values.
- Re-registering an integration preserves existing custom fields, enablement,
  timeout and executable selection. `--executable` explicitly changes its path.
- Disabled integrations cannot execute; their timeout caps each task's process
  timeout. Package versions and configuration schema versions are independent.

All configuration files and selected catalogs are preflighted before writes.
Invalid or future schemas stop the upgrade. Each changed file is backed up under
its sibling `.backups/` directory and atomically replaced with owner-only
permissions. Concurrent code-intel writers use a lock; changes detected after
preflight require a retry. Symlinked configuration files are rejected.

`--dry-run` creates no files, backups or locks. Repeating a completed upgrade makes
no further changes or backups. A filesystem failure can leave earlier files
upgraded: this is per-file atomicity, not one transaction across every file.
Backups remain available and retrying is safe. To restore a JSON configuration,
stop writers, choose the exact `.backups/<original-name>.*.bak` file, and copy it
back over that original configuration file; do not overwrite unrelated files.

## Repository catalogs

Optionally include explicit catalogs in the upgrade:

```bash
uv run code-intel upgrade-config --dry-run --repo /path/to/repository
uv run code-intel upgrade-config --repo /path/to/repository
uv run code-intel scan /path/to/repository --incremental
uv run code-intel doctor /path/to/repository --summary
```

Compatible schema-0 and schema-1 catalogs upgrade to schema 2 in a transaction.
SQLite's backup API first creates a consistent private backup, including
committed WAL data. Migration adds a full-docstring column and populates the
function BM25 index from already stored, redacted source data. Existing symbols
and usage history are preserved. Missing catalogs are reported without being
created. Unsupported legacy layouts require a scan instead; future layouts are
rejected. Legacy docstring summaries remain usable during migration; the next
scan captures full Python docstrings from source.

New scans write schema 2. The new analyzer version triggers one full refresh on
the next incremental scan, after which only changed files are reindexed.
Reconnect running MCP clients to the updated package before that refresh:
older processes reject the newer catalog schema. Until refreshed, the updated
package can still query compatible older catalogs through lexical recovery.
Analyzer changes rebuild derived indexes through
the existing atomic catalog publication path, which copies usage history before
replacement. New code-intel versions refuse to read or replace a catalog with a
newer schema marker. Avoid downgrading to older releases that predate these guards.

If command guidance changes, refresh only its managed instruction block:

```bash
uv run code-intel install-agent-notes /path/to/repository \
  --command-prefix 'uv run --project /path/to/code-intel code-intel'
```

Keep local repository paths, configuration backups, catalogs and measurement
records out of public commits. Upgrade diagnostics print counts and schema
transitions rather than configuration contents.
