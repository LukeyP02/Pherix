"""Unit tests for GitAdapter — snapshot/apply/restore against a real git repo.

Exercises the adapter directly with synthesized Effects (the pattern the other
adapter tests use). A real `git` binary is required; tests skip if absent. No
network, no remote — this adapter governs the LOCAL working tree only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pherix.core.adapters.base import ResourceAdapter, TransactionalResourceAdapter
from pherix.core.adapters.git import GitAdapter, GitHandle
from pherix.core.effects import Effect

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("v1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c1")
    (root / "app.py").write_text("v2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c2")
    return root


def _effect(index: int = 0) -> Effect:
    return Effect(
        txn_id="t", index=index, tool="run_git", args={}, resource="git",
        reversible=True,
    )


def _snap(adapter: GitAdapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


# --- protocol conformance ---------------------------------------------------


def test_git_adapter_satisfies_protocols(repo: Path):
    a = GitAdapter(repo)
    assert isinstance(a, ResourceAdapter)
    assert isinstance(a, TransactionalResourceAdapter)
    assert a.supports_rollback() is True


# --- the headline: a destroyed history is restored --------------------------


def test_hard_reset_destroying_history_is_restored(repo: Path):
    head_before = _git(repo, "rev-parse", "HEAD")
    log_before = _git(repo, "log", "--oneline")

    adapter = GitAdapter(repo)
    adapter.begin()
    try:
        effect = _effect()
        _snap(adapter, effect)

        # The agent nukes a commit off the history.
        adapter.apply(effect, lambda git: git.run("reset --hard HEAD~1"))
        assert _git(repo, "rev-parse", "HEAD") != head_before  # damage happened
        assert (repo / "app.py").read_text() == "v1\n"

        # Pherix folds the journal backward → the commit returns.
        adapter.restore(effect.snapshot)
    finally:
        adapter.rollback()

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "log", "--oneline") == log_before
    assert (repo / "app.py").read_text() == "v2\n"


# --- dirty tracked changes + untracked files round-trip ----------------------


def test_dirty_and_untracked_state_restored(repo: Path):
    # Uncommitted edit to a tracked file + a brand-new untracked file.
    (repo / "app.py").write_text("v2-wip\n")
    (repo / "notes.txt").write_text("scratch\n")

    adapter = GitAdapter(repo)
    adapter.begin()
    try:
        effect = _effect()
        _snap(adapter, effect)

        # Agent wipes the working tree clean and removes the untracked file.
        adapter.apply(effect, lambda git: git.run("reset --hard HEAD"))
        (repo / "notes.txt").unlink()
        assert (repo / "app.py").read_text() == "v2\n"  # wip lost

        adapter.restore(effect.snapshot)
    finally:
        adapter.rollback()

    assert (repo / "app.py").read_text() == "v2-wip\n"      # dirty change back
    assert (repo / "notes.txt").read_text() == "scratch\n"  # untracked back


# --- a file the agent newly creates after snapshot is cleaned on restore -----


def test_agent_created_untracked_file_removed_on_restore(repo: Path):
    adapter = GitAdapter(repo)
    adapter.begin()
    try:
        effect = _effect()
        _snap(adapter, effect)
        adapter.apply(effect, lambda git: git.run("status"))
        (repo / "leftover.tmp").write_text("junk\n")  # created after snapshot
        adapter.restore(effect.snapshot)
    finally:
        adapter.rollback()

    assert not (repo / "leftover.tmp").exists()


def test_handle_runs_git_rooted_at_repo(repo: Path):
    out = GitHandle(repo).run("rev-parse --abbrev-ref HEAD")
    assert out in ("main", "master")
