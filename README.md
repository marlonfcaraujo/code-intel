# code-intel

Repository intelligence for faster, safer code changes.

`code-intel` builds a local SQLite catalog for a repository and answers practical
engineering questions:

- where symbols live
- which files depend on a file
- which tests are likely related to a change
- which files have the highest fan-in risk
- how many file reads and tokens the tool likely avoided

The catalog is stored under `.code-intel/catalog.sqlite` by default and is ignored
by git. Symbol search can use the built-in catalog or a local jCodemunch database
when one exists for the same repository.

## Install for development

```bash
uv sync --group dev
uv run code-intel --help
```

## Commands

```bash
code-intel scan .
code-intel find WorkflowOrchestrator
code-intel find WorkflowOrchestrator --provider jcodemunch
code-intel explain src/void/api/routes.py
code-intel tests src/void/api/routes.py
code-intel risk --top 20
code-intel savings --repo .
code-intel doctor .
```

`find` defaults to `--provider auto`, which searches jCodemunch first when a
matching local database exists, then falls back to the code-intel catalog.

## Savings report

Lookup commands record a small local usage event in `.code-intel/catalog.sqlite`.
The `savings` command reports estimated saved tokens and avoided file reads:

```bash
code-intel savings --repo .
code-intel savings --repo . --json
```

The estimate compares the size of the cataloged repository to the smaller set of
files returned by a targeted lookup. It is directional, not billing-grade.

## MCP server

Install the optional MCP extra before using the server:

```bash
uv sync --extra mcp --group dev
uv run code-intel serve-mcp --repo /path/to/repo
```

Example MCP client config when running from this checkout:

```json
{
  "mcpServers": {
    "code-intel": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/Users/maraujo/git_personal/code-intel",
        "code-intel",
        "serve-mcp",
        "--repo",
        "."
      ]
    }
  }
}
```

Tools exposed by the MCP server:

- `catalog_repo`
- `find_symbols`
- `explain_file`
- `related_tests`
- `risk_report`
- `catalog_health`
- `savings_report`

To teach Claude/Codex in a repository to use the tool, upsert the managed note
into `CLAUDE.md` and `AGENTS.md`:

```bash
code-intel install-agent-notes /path/to/repo
```

When running from this source checkout without a global install, pass an explicit
command prefix:

```bash
uv run code-intel install-agent-notes /path/to/repo \
  --command-prefix "uv run --project /Users/maraujo/git_personal/code-intel code-intel"
```

## Design principles

- Keep generated catalogs out of normal commits.
- Prefer structured language parsers where available.
- Make repo-specific knowledge a plugin/config concern, not core behavior.
- Keep the CLI useful without jCodemunch, while using jCodemunch as a provider
  when a matching local database is available.
