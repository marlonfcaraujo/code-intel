<!-- code-intel-agent-notes:start -->
## Code Intel

Use `code-intel` for repository-aware navigation before non-trivial code changes.

Recommended workflow:
- Refresh the local catalog when source files changed: `uv run code-intel scan .`
- Explain a file before editing it: `uv run code-intel explain --repo . path/to/file.py`
- Find symbols without broad file reads: `uv run code-intel find --repo . SymbolName`
- Find likely related tests: `uv run code-intel tests --repo . path/to/file.py`
- Review high-risk files for broad work: `uv run code-intel risk --repo . --top 20`

The generated catalog lives in `.code-intel/catalog.sqlite`; do not commit it.
<!-- code-intel-agent-notes:end -->
