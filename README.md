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

## Design principles

- Keep generated catalogs out of normal commits.
- Prefer structured language parsers where available.
- Make repo-specific knowledge a plugin/config concern, not core behavior.
- Keep the CLI useful without jCodemunch, but allow jCodemunch-backed providers
  later when available.
