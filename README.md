# code-intel

```text
   ______          __        ____      __       __
  / ____/___  ____/ /__     /  _/___  / /____  / /
 / /   / __ \/ __  / _ \    / // __ \/ __/ _ \/ /
/ /___/ /_/ / /_/ /  __/  _/ // / / / /_/  __/ /
\____/\____/\__,_/\___/  /___/_/ /_/\__/\___/_/
```

<p align="center">
  <strong>Self-contained repository intelligence for faster AI-assisted code changes.</strong>
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB">
  <img alt="SQLite catalog" src="https://img.shields.io/badge/catalog-SQLite-003B57">
  <img alt="MCP ready" src="https://img.shields.io/badge/MCP-ready-4B5563">
  <img alt="No external service" src="https://img.shields.io/badge/external%20service-not%20required-15803D">
  <img alt="Status" src="https://img.shields.io/badge/status-local%20tool-0F172A">
</p>

`code-intel` builds a local SQLite catalog for a repository, then answers the
questions coding agents usually waste time and tokens trying to discover by
scanning files.

It is designed for large codebases where Codex, Claude Code, or a human engineer
needs quick answers before editing:

- where a function, class, method, constant, or type lives
- which files depend on a file
- which tests are likely related to a change
- which files have the highest blast-radius risk
- how much context the targeted lookup likely avoided loading

Normal use is self-contained. `code-intel` creates and reads its own local
catalog at `.code-intel/catalog.sqlite`. It does not require jCodemunch, a hosted
service, or a background daemon.

## Quickstart

From this checkout:

```bash
cd /Users/maraujo/git_personal/code-intel
uv sync --group dev
uv run code-intel --help
```

Catalog any repository:

```bash
uv run code-intel scan /Users/maraujo/git/vme_bmaas
```

Search the generated catalog:

```bash
uv run code-intel find --repo /Users/maraujo/git/vme_bmaas get_database_url
```

Check impact before editing a file:

```bash
uv run code-intel explain --repo /Users/maraujo/git/vme_bmaas alembic/env.py
uv run code-intel tests --repo /Users/maraujo/git/vme_bmaas alembic/env.py
uv run code-intel risk --repo /Users/maraujo/git/vme_bmaas --top 20
```

Show the usage and estimated savings report:

```bash
uv run code-intel savings --repo /Users/maraujo/git/vme_bmaas
```

## What It Creates

Running `scan` creates this local file inside the target repository:

```text
<repo>/.code-intel/catalog.sqlite
```

The SQLite catalog contains:

| Table | Purpose |
| --- | --- |
| `meta` | repo path, generated time, file count |
| `files` | discovered source files, language, line count, byte size |
| `symbols` | names, kinds, paths, line numbers, signatures, docs |
| `dependencies` | resolved and unresolved import edges |
| `usage_events` | lookup events used for savings reports |

The catalog is generated data. Keep it out of commits.

For a project-local ignore without changing tracked files:

```bash
printf '\n.code-intel/\n' >> /path/to/repo/.git/info/exclude
```

For a shared project ignore:

```bash
echo ".code-intel/" >> /path/to/repo/.gitignore
```

## Commands

### `scan`

Build or refresh the catalog.

```bash
code-intel scan [REPO]
code-intel scan /Users/maraujo/git/vme_bmaas
```

If the database does not exist, `scan` creates `.code-intel/` and
`catalog.sqlite`. If it already exists, `scan` rebuilds it from the current
source tree.

### `find`

Find cataloged symbols without scanning the whole repo.

```bash
code-intel find --repo /path/to/repo SymbolName
code-intel find --repo /path/to/repo SymbolName --limit 5
```

Default provider is the built-in `catalog` provider. If the catalog is missing,
`find` exits with a clear message telling you to run `scan` first.

Optional compatibility provider:

```bash
code-intel find --repo /path/to/repo SymbolName --provider jcodemunch
```

The `jcodemunch` provider only reads an existing local jCodemunch SQLite
database when explicitly requested. It is not required for normal operation.

### `explain`

Show the likely impact of changing a file.

```bash
code-intel explain --repo /path/to/repo src/app/service.py
code-intel explain --repo /path/to/repo src/app/service.py --json
```

The report includes direct dependents, transitive dependents, related tests, a
blast score, and a risk label.

### `tests`

Find tests likely related to a source file.

```bash
code-intel tests --repo /path/to/repo src/app/service.py
```

Signals include matching file names, imports, and public source symbols
mentioned in test files.

### `risk`

Rank source files by blast-radius risk.

```bash
code-intel risk --repo /path/to/repo --top 20
```

Use this before broad refactors or when deciding where extra test coverage
matters most.

### `savings`

Report lookup usage and estimated avoided context.

```bash
code-intel savings --repo /path/to/repo
code-intel savings --repo /path/to/repo --json
```

The estimate compares the cataloged repository size to the smaller set of files
returned by targeted lookups. It is directional, not billing-grade.

### `doctor`

Inspect repository and catalog health.

```bash
code-intel doctor /path/to/repo
```

Use this when an agent is unsure whether a repo has been scanned.

## MCP Server

Install optional MCP dependencies:

```bash
uv sync --extra mcp --group dev
```

Run the MCP server over stdio:

```bash
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
        "/path/to/repo"
      ]
    }
  }
}
```

MCP tools:

| Tool | Purpose |
| --- | --- |
| `catalog_repo` | build or refresh the catalog |
| `find_symbols` | search symbols from the catalog |
| `explain_file` | explain change impact for one file |
| `related_tests` | return likely test files for one source file |
| `risk_report` | list highest-risk files |
| `catalog_health` | report catalog status and counts |
| `savings_report` | report usage and estimated savings |

Recommended agent behavior:

1. Call `catalog_health`.
2. If `catalog_exists` is false, call `catalog_repo`.
3. Use `find_symbols`, `explain_file`, `related_tests`, and `risk_report`
   before loading broad file context.
4. Use `savings_report` when you want evidence that the tool reduced context.

## Agent Notes

`code-intel` can upsert a small managed instruction block into `CLAUDE.md` and
`AGENTS.md` so Claude Code and Codex know how to use it.

From this checkout:

```bash
uv run code-intel install-agent-notes /path/to/repo \
  --command-prefix "uv run --project /Users/maraujo/git_personal/code-intel code-intel"
```

The generated section is bounded by comment markers, so re-running the command
updates the same block instead of appending duplicates.

## AI Workflow Comparison

| Task | Without code-intel | With code-intel |
| --- | --- | --- |
| Find a symbol | grep or scan many files | one symbol lookup with path and line |
| Understand change impact | manually trace imports | `explain` or `explain_file` |
| Pick tests | guess from file names | `tests` or `related_tests` |
| Find risky files | broad manual inspection | `risk` or `risk_report` |
| Measure value | anecdotal | `savings` or `savings_report` |

## Supported Source Types

Current built-in analyzers:

| Language | Extensions | Symbol extraction | Dependency extraction |
| --- | --- | --- | --- |
| Python | `.py` | AST-based functions, classes, methods | AST imports and relative imports |
| JavaScript | `.js`, `.mjs`, `.cjs`, `.jsx` | conservative regex functions, classes, constants, types | import, export-from, require, dynamic import |
| TypeScript | `.ts`, `.tsx` | conservative regex functions, classes, constants, types | import, export-from, require, dynamic import |

The analyzer set is intentionally conservative. Add new languages as focused
providers instead of making the core scanner guess.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Useful local smoke test:

```bash
uv run code-intel scan .
uv run code-intel find --repo . CatalogStore --limit 3
uv run code-intel savings --repo .
```

## Design Principles

- Stay self-contained by default.
- Keep generated catalogs out of commits.
- Prefer structured parsers where available.
- Keep agent instructions concise and managed.
- Make optional compatibility providers explicit.
- Favor targeted context over broad file loading.
