# Changelog

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
