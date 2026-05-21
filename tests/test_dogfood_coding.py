"""Mechanism test (mocked/simulated, deterministic, CI) for the coding sandbox.

This is NOT a real-agent run — it is a mechanism test of the environment-level
interception. A coding CLI uses built-in Edit/Write/Bash that MCP cannot
intercept. The
sandbox governs at the *environment* level instead: a Pherix CoW filesystem
overlay + routed git/shell. These tests SIMULATE a CLI's built-in tool calls
(the exact stream a real Claude Code / Cursor / Goose run would emit) hitting
:meth:`Sandbox.route`, and assert Pherix's three guarantees hold *regardless of
which agent drives them*:

  * allowed edits under ``src/**`` are journalled + applied to the real tree;
  * a forbidden action (write to ``/etc``, write a secret, push to main,
    over-spend the shell cap) is GATED — denied, nothing journalled, the real
    resource untouched;
  * every action is audited under a ``client_id``.

No LLM, no key, no network — the sandbox routing layer is pure mechanism. A real
out-of-box CLI run is the manual capstone (see the dogfood README); this is the
deterministic proof that the interception works for *any* agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.dogfood.coding.sandbox import (
    VERB_GIT,
    VERB_SHELL,
    VERB_WRITE,
    Sandbox,
    write_shims,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo-shaped tree (no real git needed for the mechanism test)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('v1')\n")
    (tmp_path / "README.md").write_text("# project\n")
    return tmp_path


# --- allowed edits: journalled + applied to the real tree ------------------


def test_allowed_src_edit_is_journalled_and_applied(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        out = sb.route(VERB_WRITE, path="src/app.py", content="print('v2')\n")
        assert out.allowed and out.journalled
        # Applied live to the real overlay tree (reversible FS lane).
        assert (repo / "src" / "app.py").read_text() == "print('v2')\n"
        # One journalled effect, APPLIED.
        eff = sb._ctx.txn.effects[-1]
        assert eff.tool == "sandbox_write"
        assert eff.status.name == "APPLIED"
        sb._ctx.rollback()  # keep the tree pristine; the audit holds the record


def test_new_src_file_create_is_allowed(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        out = sb.route(VERB_WRITE, path="src/new.py", content="x = 1\n")
        assert out.allowed and out.journalled
        assert (repo / "src" / "new.py").exists()
        sb._ctx.rollback()


def test_rollback_restores_the_tree(repo: Path) -> None:
    """A reversible FS edit is undone on rollback — nothing leaks if the session fails."""
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        sb.route(VERB_WRITE, path="src/app.py", content="MUTATED\n")
        assert (repo / "src" / "app.py").read_text() == "MUTATED\n"
        sb._ctx.rollback()
    # Back to the seeded content — the CoW overlay restored it.
    assert (repo / "src" / "app.py").read_text() == "print('v1')\n"


# --- forbidden actions: GATED, nothing journalled, resource untouched ------


def test_write_to_etc_is_gated(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        n_before = len(sb._ctx.txn.effects)
        out = sb.route(VERB_WRITE, path="/etc/passwd", content="pwned\n")
        assert not out.allowed
        assert not out.journalled
        assert "GATED" in out.detail
        # Stage-time deny journals nothing.
        assert len(sb._ctx.txn.effects) == n_before
        sb._ctx.rollback()
    # The gate fired before any adapter touched disk — no /etc write happened.
    # (We assert via the journal above; reading /etc directly is environment-
    # dependent and unnecessary — nothing was ever routed to the adapter.)


def test_write_to_secret_is_gated(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        out = sb.route(VERB_WRITE, path=".env", content="SECRET=x\n")
        assert not out.allowed and not out.journalled
        assert not (repo / ".env").exists()
        sb._ctx.rollback()


def test_edit_outside_src_is_gated(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        out = sb.route(VERB_WRITE, path="README.md", content="hacked\n")
        assert not out.allowed and not out.journalled
        # The real README was not modified.
        assert (repo / "README.md").read_text() == "# project\n"
        sb._ctx.rollback()


def test_push_to_main_is_gated(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        # A commit is allowed (local state)...
        commit = sb.route(VERB_GIT, argv=["commit", "-m", "feat"])
        assert commit.allowed and commit.journalled
        # ...but a push to main is gated (the publish boundary).
        push = sb.route(VERB_GIT, argv=["push", "origin", "main"])
        assert not push.allowed and not push.journalled
        assert "push" in push.detail
        sb._ctx.rollback()


def test_shell_spend_cap_gates_the_fourth_call(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        for i in range(3):
            out = sb.route(VERB_SHELL, argv=["-c", f"cmd{i}"])
            assert out.allowed and out.journalled
        # The cap is max=3; the fourth shell call is denied.
        over = sb.route(VERB_SHELL, argv=["-c", "cmd3"])
        assert not over.allowed and not over.journalled
        assert "cap" in over.detail.lower()
        sb._ctx.rollback()


# --- audit: every action attributed to a client_id -------------------------


def test_run_is_audited_with_client_id(repo: Path) -> None:
    sandbox = Sandbox(root=repo, client_id="claude-code@box-7")
    with sandbox.session() as sb:
        sb.route(VERB_WRITE, path="src/app.py", content="print('v2')\n")
        sb.route(VERB_GIT, argv=["commit", "-m", "feat"])
        sb.route(VERB_SHELL, argv=["-c", "ls"])
        txn_id = sb._ctx.txn_id
        sb._ctx.rollback()
    # The transaction row carries the client_id provenance.
    row = sandbox.audit.get_transaction(txn_id)
    assert row is not None
    assert row["client_id"] == "claude-code@box-7"
    # And the journalled effects are all attributed to this txn.
    effects = sandbox.audit.get_effects(txn_id)
    tools = [e["tool"] for e in effects]
    assert tools == ["sandbox_write", "sandbox_git", "sandbox_shell"]


def test_gated_action_leaves_no_audit_effect(repo: Path) -> None:
    """A denied action writes no effect row — the audit only records what happened."""
    sandbox = Sandbox(root=repo, client_id="agent-A")
    with sandbox.session() as sb:
        sb.route(VERB_WRITE, path="src/ok.py", content="x=1\n")  # journalled
        sb.route(VERB_WRITE, path="/etc/passwd", content="pwned\n")  # gated
        txn_id = sb._ctx.txn_id
        sb._ctx.rollback()
    effects = sandbox.audit.get_effects(txn_id)
    # Only the allowed write is in the audit; the gated one never landed.
    assert len(effects) == 1
    assert effects[0]["tool"] == "sandbox_write"
    assert "/etc" not in effects[0]["args"]


# --- agent-agnosticism: the same sandbox governs two different "CLIs" ------


def test_two_different_agents_same_sandbox_mechanism(repo: Path) -> None:
    """The interception is agent-agnostic: distinct client_ids, same governance.

    Simulates the SAME built-in action stream coming from two different
    out-of-box CLIs (e.g. Claude Code and an open-source agent). Each is
    governed identically — allowed edits apply, the forbidden push gates — and
    each run is independently attributed. This is the neutrality moat: the
    sandbox does not know or care which agent drives it.
    """
    for agent in ("claude-code", "goose-oss"):
        sandbox = Sandbox(root=repo, client_id=agent)
        with sandbox.session() as sb:
            assert sb.route(VERB_WRITE, path="src/f.py", content="y=2\n").allowed
            assert not sb.route(VERB_GIT, argv=["push", "origin", "main"]).allowed
            txn_id = sb._ctx.txn_id
            sb._ctx.rollback()
        assert sandbox.audit.get_transaction(txn_id)["client_id"] == agent


# --- shims: real executables on PATH for the manual CLI run ----------------


def test_shims_are_written_as_real_executables(tmp_path: Path) -> None:
    """The PATH shims a real CLI run needs are real, executable files.

    The offline test routes via :meth:`Sandbox.route`; a real CLI shells out by
    name, so the sandbox must plant executable ``git``/``sh`` shims on PATH.
    Assert they exist and are executable (the manual run depends on this).
    """
    import os

    bin_dir = tmp_path / "bin"
    shims = write_shims(bin_dir)
    assert set(shims) == {"git", "sh"}
    for path in shims.values():
        assert path.exists()
        assert os.access(path, os.X_OK)
        assert "route-cli" in path.read_text()
