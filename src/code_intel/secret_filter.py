"""Secret-path filtering and line redaction helpers."""

from __future__ import annotations

import re
from pathlib import Path

REDACTED_SECRET = "[REDACTED_SECRET]"

_SECRET_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "kubeconfig",
    }
)
_SECRET_FILE_SUFFIXES = (
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
)
_SECRET_PATH_PARTS = frozenset({".aws", ".gnupg", ".kube", ".ssh"})

_PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_MARKER_RE = re.compile(r"-----(?:BEGIN|END) [A-Z0-9 ]*PRIVATE KEY-----")
_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED_SECRET),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"), REDACTED_SECRET),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), REDACTED_SECRET),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), REDACTED_SECRET),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), REDACTED_SECRET),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED_SECRET),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{20,}"), rf"\1{REDACTED_SECRET}"),
)
_SECRET_NAME_PATTERN = (
    r"[\w.-]*(?:secret|token|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)[\w.-]*"
)
_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>\b{_SECRET_NAME_PATTERN}\b\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>[^'\"\n]{8,})(?P=quote)"
)
_UNQUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>\b{_SECRET_NAME_PATTERN}\b\s*[:=]\s*)"
    r"(?P<value>[A-Za-z0-9_./+=:@-]{16,})"
)


def is_secret_path(path: str | Path) -> bool:
    """Return true when a path is likely to contain secrets.

    Args:
        path: Repository-relative or absolute file path.

    Returns:
        True for known secret-bearing filenames, suffixes, or directories.
    """
    normalized = Path(path).as_posix()
    parts = [part.lower() for part in normalized.split("/") if part]
    if any(part in _SECRET_PATH_PARTS for part in parts):
        return True
    if not parts:
        return False

    name = parts[-1]
    if name in _SECRET_FILE_NAMES or name.startswith(".env."):
        return True
    return name.endswith(_SECRET_FILE_SUFFIXES)


def redact_secret_line(line: str) -> str:
    """Redact secret-looking values from a single source line.

    Args:
        line: Source line to sanitize.

    Returns:
        The line with recognized secret values replaced by a redaction marker.
    """
    if _PRIVATE_KEY_MARKER_RE.search(line):
        return REDACTED_SECRET

    redacted = line
    for pattern, replacement in _TOKEN_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    redacted = _QUOTED_ASSIGNMENT_RE.sub(_quoted_assignment_replacement, redacted)
    redacted = _UNQUOTED_ASSIGNMENT_RE.sub(_unquoted_assignment_replacement, redacted)
    return redacted


def redact_source_text(source: str) -> str:
    """Redact secret-looking values from source text while preserving lines.

    Args:
        source: Full source text.

    Returns:
        Source text with private-key blocks and secret-looking values redacted.
    """
    redacted_lines: list[str] = []
    in_private_key_block = False
    for line in source.splitlines():
        if in_private_key_block:
            redacted_lines.append(REDACTED_SECRET)
            if _PRIVATE_KEY_END_RE.search(line):
                in_private_key_block = False
            continue
        if _PRIVATE_KEY_BEGIN_RE.search(line):
            redacted_lines.append(REDACTED_SECRET)
            in_private_key_block = not _PRIVATE_KEY_END_RE.search(line)
            continue
        redacted_lines.append(redact_secret_line(line))
    return "\n".join(redacted_lines)


def _quoted_assignment_replacement(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{match.group('quote')}{REDACTED_SECRET}{match.group('quote')}"


def _unquoted_assignment_replacement(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{REDACTED_SECRET}"
