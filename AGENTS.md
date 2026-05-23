<!-- code-intel-agent-notes:start -->
## Code Intel

Use code-intel for self-contained repository-aware navigation before non-trivial code changes.
Prefer the MCP server tools when configured: `catalog_repo`, `find_symbols`, `explain_file`, `related_tests`, `risk_report`, and `savings_report`.
CLI fallback: refresh with `uv run code-intel scan .`, then use `uv run code-intel explain --repo . path/to/file.py` or `uv run code-intel find --repo . SymbolName`; check impact with `uv run code-intel savings --repo .`.
The generated catalog is `.code-intel/catalog.sqlite`; do not commit it.
<!-- code-intel-agent-notes:end -->
