"""Versioned configuration upgrades with private backups and atomic writes."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

CONFIG_VERSIONS = {"workspaces": 1, "benchmark-suites": 1, "integrations": 2}


def migrate_config(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Return an upgraded configuration without mutating user values.

    Args:
        payload: Existing JSON object; an absent version denotes legacy schema 0.
        kind: Managed configuration family.

    Returns:
        New dictionary including defaults and the current schema version.

    Raises:
        ValueError: On malformed, unsupported or future schemas.
    """
    if kind not in CONFIG_VERSIONS or not isinstance(payload, dict):
        raise ValueError("Unsupported configuration family or document")
    version = payload.get("schema_version", 0)
    if type(version) is not int or not 0 <= version <= CONFIG_VERSIONS[kind]:
        raise ValueError("Unsupported configuration schema; upgrade the package first")
    result = dict(payload)
    if kind == "integrations":
        if result.get("integration") not in ("codex", "hermes", "paseo", "opencode", "claude"):
            raise ValueError("Invalid integration identity")
        if not isinstance(result.get("executable"), str) or not result["executable"]:
            raise ValueError("Integration executable is required")
        result.setdefault("enabled", True)
        result.setdefault("timeout_seconds", 300)
        if type(result["enabled"]) is not bool:
            raise ValueError("Integration enabled must be a boolean")
        if type(result["timeout_seconds"]) is not int or result["timeout_seconds"] <= 0:
            raise ValueError("Integration timeout must be a positive integer")
    else:
        repos = result.get("repos")
        if not isinstance(repos, list) or not repos or not all(isinstance(repo, str) and repo for repo in repos):
            raise ValueError("Configuration repositories must be a nonempty string list")
    result["schema_version"] = CONFIG_VERSIONS[kind]
    return result


def read_config(path: Path, kind: str) -> dict[str, Any]:
    """Load a configuration with current defaults without changing the file.

    Args:
        path: Existing local configuration file.
        kind: Managed configuration family.

    Returns:
        Validated configuration, including default values and unknown keys.
    """
    return migrate_config(json.loads(path.read_text()), kind)


@contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock.sqlite")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(lock_path, timeout=30, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _atomic_bytes(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def backup_path(path: Path) -> Path:
    """Create a private backup directory and return a unique backup name."""
    directory = path.parent / ".backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"{path.name}.{stamp}.{uuid4().hex}.bak"


def update_config(
    path: Path, kind: str, updates: dict[str, Any] | None = None, *, expected: bytes | None = None
) -> bool:
    """Merge explicit changes while retaining unrelated keys and a backup.

    Args:
        path: Configuration destination; symlinks are rejected.
        kind: Managed configuration family.
        updates: Only keys explicitly changed by the caller.
        expected: Optional preflight contents, checked before any write.

    Returns:
        True when the configuration changed, false for an idempotent no-op.

    Raises:
        ValueError: On unsupported schemas, symlinks or concurrent modification.
    """
    with _config_lock(path):
        if path.is_symlink():
            raise ValueError("Configuration symlinks cannot be migrated")
        original = path.read_bytes() if path.exists() else None
        if expected is not None and original != expected:
            raise ValueError("Configuration changed after preflight; retry the upgrade")
        before = json.loads(original) if original is not None else {}
        # Validate the original version before allowing updates to overwrite it.
        if original is not None:
            migrate_config(before, kind)
        after = migrate_config({**before, **(updates or {})}, kind)
        if after == before:
            return False
        content = (json.dumps(after, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        if original is not None:
            _atomic_bytes(backup_path(path), original)
        _atomic_bytes(path, content)
        return True


def upgrade_configs(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Preflight all managed configuration files, then upgrade changed files.

    Args:
        root: Local configuration root, normally ~/.code-intel.
        dry_run: Validate and report without creating files or backups.

    Returns:
        Anonymous counts and schema transitions; no private names or contents.
    """
    plans = []
    summary = []
    for kind in CONFIG_VERSIONS:
        for path in sorted((root / kind).glob("*.json")):
            if path.is_symlink():
                raise ValueError("Configuration symlinks cannot be migrated")
            original = path.read_bytes()
            before = json.loads(original)
            after = migrate_config(before, kind)
            changed = before != after
            summary.append(
                {
                    "kind": kind,
                    "from_version": before.get("schema_version", 0),
                    "to_version": after["schema_version"],
                    "changed": changed,
                }
            )
            if changed:
                plans.append((path, kind, original))
    if not dry_run:
        for path, kind, original in plans:
            update_config(path, kind, expected=original)
    return {
        "dry_run": dry_run,
        "examined": len(summary),
        "changes": len(plans),
        "backups_created": 0 if dry_run else len(plans),
        "configurations": summary,
    }


def register_upgrade_config(subparsers: argparse._SubParsersAction) -> None:
    """Register explicit configuration and catalog upgrade commands."""
    parser = subparsers.add_parser("upgrade-config", help="Upgrade local configuration with backups")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".code-intel")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo", type=Path, action="append", default=[], help="Also upgrade a repository catalog")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_command)


def _command(args: argparse.Namespace) -> int:
    from code_intel.catalog_upgrade import upgrade_catalog

    try:
        # Validate all database and configuration plans before modifying either.
        upgrade_configs(args.config_dir, dry_run=True)
        paths = list(dict.fromkeys(repo.resolve() / ".code-intel/catalog.sqlite" for repo in args.repo))
        for path in paths:
            upgrade_catalog(path, dry_run=True)
        result = upgrade_configs(args.config_dir, dry_run=args.dry_run)
        result["catalogs"] = [upgrade_catalog(path, dry_run=args.dry_run) for path in paths]
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error):
        raise ValueError("Upgrade failed; check local configuration schemas and catalog compatibility") from None
