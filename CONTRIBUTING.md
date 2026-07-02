# Contributing

Thanks for improving code-intel. The project is intentionally small and local-first:
changes should keep repository indexing fast, deterministic, and useful for coding
agents before they read broad file context.

## Development Setup

```bash
git clone https://github.com/marlonfcaraujo/code-intel.git
cd code-intel
uv sync --group dev --frozen
```

Run the CLI from the checkout:

```bash
uv run code-intel --help
```

## Quality Checks

Run these before opening a pull request:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
```

For changes that affect indexing, lookup ranking, workspace context, or token
payload size, include a benchmark or workflow-benchmark result in the PR:

```bash
uv run code-intel scan . --incremental --skip-unchanged-meta --json
uv run code-intel workflow-benchmark --workspace void --query SurfacePanel --source-first --json --summary
```

## Pull Request Expectations

- Keep generated catalogs out of commits.
- Update tests for changed behavior.
- Update README examples or metrics when user-facing commands change.
- Prefer small, reviewable changes over broad refactors.
- Do not add hosted-service dependencies to the default path.
- Explain any benchmark regression and why it is worth the tradeoff.

## Commit Style

Use concise, descriptive commit messages. A good message names the behavior
changed and the reason it matters, for example:

```text
Add workspace context batching for repeated queries
```
