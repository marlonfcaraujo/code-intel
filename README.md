# code-intel

Repository intelligence for faster, safer code changes.

`code-intel` builds a local SQLite catalog for a repository and answers practical
engineering questions:

- where symbols live
- which files depend on a file
- which tests are likely related to a change
- which files have the highest fan-in risk

The catalog is stored under `.code-intel/catalog.sqlite` by default and is ignored
by git. The tool is designed to work as a standalone CLI first, with provider and
repo-specific plugin hooks added over time.

## Install for development

```bash
uv sync --group dev
uv run code-intel --help
```

## Commands

```bash
code-intel scan .
code-intel find WorkflowOrchestrator
code-intel explain src/void/api/routes.py
code-intel tests src/void/api/routes.py
code-intel risk --top 20
code-intel doctor .
```

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
- Keep the CLI useful without jCodemunch, but allow jCodemunch-backed providers
  later when available.
