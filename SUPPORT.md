# Support

Thanks for using `code-intel`.

## Where to get help

- [GitHub Discussions](https://github.com/marlonfcaraujo/code-intel/discussions): usage questions, setup help, and general tips.
- [GitHub Issues](https://github.com/marlonfcaraujo/code-intel/issues): reproducible bugs and feature requests.
- `CONTRIBUTING.md`: contributor workflow, local setup, and pull request expectations.

## Reporting a bug

When opening an issue, include:

- `code-intel --version`
- operating system and Python version
- command used (full command and flags)
- repository size and type (e.g. language mix, monorepo/submodule)
- a minimal reproduction path or steps
- logs from the command (for benchmark/json outputs, include the JSON payload when possible)

## Requesting a feature

- explain the workflow gap the feature would solve
- include expected command UX (new command/flags/output format)
- include a small example repository shape where it helps

## Security issues

Do not include private secrets in public issues.

- For sensitive vulnerability reports, follow `SECURITY.md`.

## FAQ

### `code-intel` command is not found

Re-run installation so your shell has the executable:

```bash
uv tool install /path/to/code-intel
```

If the command is still unavailable, restart the shell session and verify `$PATH`
contains uv’s default tool location.

### `scan` seems to do nothing

Run the scan in non-incremental mode once to initialize the catalog, then rerun:

```bash
code-intel scan /path/to/repo
code-intel scan /path/to/repo --incremental --skip-unchanged-meta
```

### Catalog files accidentally showing up in git status

Add a local ignore for generated catalogs:

```bash
printf '\n.code-intel/\n' >> /path/to/repo/.git/info/exclude
```

If multiple collaborators need the same rule, keep it in `.gitignore`.
