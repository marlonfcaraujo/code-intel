# Changelog

## 0.4.0

- Add a field-weighted SQLite FTS5 BM25 index over names, signatures, docstrings
  and bounded function bodies, with identifier splitting and no new dependency.
- Preserve exact lookup fast paths and maintain function documents during
  incremental updates and deletions. Existing context-window deduplication is reused.
- Add catalog schema 2 migration with backups and retained usage history.
- Capture bounded full Python docstrings and multiline declaration signatures.
- Publish a larger public pytest pilot and index-overhead measurements, including
  cold-index costs and the observed increase in uncached input tokens.

## 0.3.0

- Recover empty sentence lookups with bounded lexical keywords, ranked using
  symbol metadata and indexed body evidence. Preserve exact-identifier behavior
  and explicit symbol/text search controls.
- Return docstring summaries and optional bounded indexed excerpts from MCP
  lookup, with guidance when no result is found.
- Clarify lexical query guidance in MCP tools and managed agent instructions.
- Add retrieval regression fixtures and a second public Codex pilot, retaining
  the original report and showing task-specific improvements and regressions.

## 0.2.1

- Fix private native usage imports failing when opening their output file.
- Preserve reported Codex cache-write tokens and use the final agent message
  for grading instead of prepending progress commentary.
- Add a reproducible public-source Codex pilot and publish measured counters,
  including the limitations that prevent claiming causal or billed savings.

## 0.2.0

- Add measured task reports for input, output and cache usage, with missing
  metrics represented explicitly and paired-task success checks.
- Add local registrations and native usage adapters for Codex, Hermes, OpenCode
  and Claude Code. Paseo registration reports the limitation of its latest-usage
  snapshot instead of treating it as complete task accounting.
- Add configuration upgrades with dry-run validation, schema versions, private
  backups, atomic writes and preservation of unknown fields.
- Add compatible catalog schema migration and guards against future schemas,
  preserving usage history through migration and index replacement.
- Add integration enablement and timeout defaults and a CLI version command.
- Document setup, upgrades, experimental controls and privacy boundaries.
