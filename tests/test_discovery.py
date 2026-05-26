from __future__ import annotations

from pathlib import Path

from code_intel.discovery import discover_source_files


def test_discovery_skips_nested_worktree_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".worktree/temporal_migration/src").mkdir(parents=True)
    (repo / ".worktrees/feature_branch/src").mkdir(parents=True)
    (repo / ".claude/worktrees/agent/src").mkdir(parents=True)
    (repo / "src/app.py").write_text("def app() -> None:\n    pass\n")
    (repo / ".worktree/temporal_migration/src/worktree_app.py").write_text("def worktree_app() -> None:\n    pass\n")
    (repo / ".worktrees/feature_branch/src/feature_app.py").write_text("def feature_app() -> None:\n    pass\n")
    (repo / ".claude/worktrees/agent/src/agent_app.py").write_text("def agent_app() -> None:\n    pass\n")

    files = discover_source_files(repo, frozenset({".py"}))

    assert [path.relative_to(repo).as_posix() for path in files] == ["src/app.py"]


def test_discovery_catalogs_worktree_when_it_is_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees/temporal_migration"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src/worktree_app.py").write_text("def worktree_app() -> None:\n    pass\n")

    files = discover_source_files(worktree, frozenset({".py"}))

    assert [path.relative_to(worktree).as_posix() for path in files] == ["src/worktree_app.py"]
