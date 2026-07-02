# Security Policy

## Supported Versions

Security fixes are handled on the default branch. Until the project publishes
versioned releases, use the latest commit from `main`.

## Reporting a Vulnerability

Please do not open a public issue for a vulnerability.

Use GitHub private vulnerability reporting if it is enabled for the repository.
If that is unavailable, contact the maintainer privately and include:

- affected version or commit
- impact and exploitability notes
- reproduction steps
- any suggested fix

Maintainers will acknowledge valid reports and coordinate a fix before public
disclosure when possible.

## Scope

In scope:

- vulnerabilities in catalog generation, CLI behavior, or MCP responses
- unsafe handling of local repository paths
- unintended exposure of cataloged source content

Out of scope:

- issues requiring write access to the local machine
- vulnerabilities in third-party tools outside this repository
- reports without enough detail to reproduce or assess impact
