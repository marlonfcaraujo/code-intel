from __future__ import annotations

import subprocess
from pathlib import Path

from code_intel import discovery
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


def test_discovery_skips_secret_like_paths_even_when_extension_is_supported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".ssh").mkdir()
    (repo / "src/app.py").write_text("def app() -> None:\n    pass\n")
    (repo / ".env.py").write_text("TOKEN = 'sk-1234567890abcdefghijklmnopqrstuvwxyz'\n")
    (repo / ".ssh/id_rsa.py").write_text("TOKEN = 'sk-1234567890abcdefghijklmnopqrstuvwxyz'\n")

    files = discover_source_files(repo, frozenset({".py"}))

    assert [path.relative_to(repo).as_posix() for path in files] == ["src/app.py"]


def test_discovery_catalogs_worktree_when_it_is_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".worktrees/temporal_migration"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src/worktree_app.py").write_text("def worktree_app() -> None:\n    pass\n")

    files = discover_source_files(worktree, frozenset({".py"}))

    assert [path.relative_to(worktree).as_posix() for path in files] == ["src/worktree_app.py"]


def test_discovery_uses_git_from_nested_repo_subdirectory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ui_src = repo / "ui/src"
    ui_src.mkdir(parents=True)
    (repo / ".gitignore").write_text("ui/src/ignored.js\n")
    (ui_src / "App.jsx").write_text("export function App() { return null; }\n")
    (ui_src / "ignored.js").write_text("export const ignored = true;\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)

    files = discover_source_files(ui_src, frozenset({".js", ".jsx"}))

    assert [path.relative_to(ui_src).as_posix() for path in files] == ["App.jsx"]


def test_discovery_falls_back_to_walk_when_git_times_out(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("def app() -> None:\n    pass\n")

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(discovery.subprocess, "run", timeout_run)

    files = discover_source_files(repo, frozenset({".py"}))

    assert [path.relative_to(repo).as_posix() for path in files] == ["src/app.py"]
