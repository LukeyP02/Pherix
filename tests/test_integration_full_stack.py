"""Cross-component integration — the whole stack in one flow.

The unit suite proves each layer in isolation: the SQL adapter's savepoints, the
FS adapter's copy-on-write, the policy fold, the audit rows, the dry-run
state-diff. These tests prove they compose — a single ``agent_txn`` that touches
*two* adapters (``sql`` + ``fs``) under a *real* multi-rule :class:`Policy`,
writing into a *real on-disk* :class:`AuditJournal`, and asserting the
end-to-end invariants the slices each promise locally:

- a clean commit persists both resources AND the audit shows COMMITTED with one
  row per effect, attributed by ``client_id``;
- a mid-body exception rolls the *whole* transaction back — SQL row gone, file
  restored — across both adapters at once (the backward fold is resource-blind);
- a commit-time policy denial (a spend cap exceeded) unwinds everything, marks
  the offending effect GATED in the audit, and leaves neither resource changed;
- a dry-run over the same two adapters reports the structural ``state_diff`` for
  both and persists nothing.

Fully offline: the engine dispatches ``@tool`` functions directly — no LLM, no
network, no key. Tools are registered *inside* each test (the autouse
``conftest`` fixture clears the global REGISTRY around every test), never at
module scope.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.policy import Cap, Deny, Allow, Policy
from pherix.core.runtime import agent_txn
from pherix.core.dry_run import dry_run
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


@pytest.fixture
def stack():
    """A two-adapter stack over real on-disk infra + an on-disk audit journal.

    Yields ``(adapters, audit, db_path, fs_root)``. SQLite is on-disk (not
    ``:memory:``) so commit durability is verifiable by re-opening the file;
    the audit journal is on-disk for the same reason. Everything is torn down
    on exit.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="pherix_it_")
    os.close(fd)
    fs_root = Path(tempfile.mkdtemp(prefix="pherix_it_tree_"))
    audit_fd, audit_path = tempfile.mkstemp(suffix=".db", prefix="pherix_it_audit_")
    os.close(audit_fd)

    conn = sqlite3.connect(db_path, isolation_level=None)
    # WAL so a second connection can write while another holds a txn open —
    # what the cross-connection isolation test needs without deadlocking.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance INTEGER)")
    sql_adapter = SQLiteAdapter(conn)
    fs_adapter = FilesystemAdapter(fs_root)
    audit = AuditJournal(audit_path)
    adapters = {"sql": sql_adapter, "fs": fs_adapter}
    try:
        yield adapters, audit, db_path, fs_root
    finally:
        conn.close()
        audit.close()
        import shutil

        shutil.rmtree(fs_root, ignore_errors=True)
        for p in (db_path, audit_path):
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.unlink(p + suffix)
                except FileNotFoundError:
                    pass


def _balances(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        return list(conn.execute("SELECT id, balance FROM accounts ORDER BY id"))
    finally:
        conn.close()


def _register_tools():
    """Register the sql + fs tools used across these tests; return the wrappers."""

    @tool(resource="sql")
    def open_account(conn, account_id, balance):
        """Open an account with a starting balance."""
        execute_isolated(
            conn,
            "INSERT INTO accounts (id, balance) VALUES (?, ?)",
            (account_id, balance),
            writes=[("accounts", account_id)],
        )
        return {"account_id": account_id, "balance": balance}

    @tool(resource="fs")
    def write_receipt(fs, path, body):
        """Write a receipt file recording an account event."""
        fs.write(path, body.encode())
        return path

    return open_account, write_receipt


def test_multi_adapter_commit_persists_both_and_audits(stack):
    adapters, audit, db_path, fs_root = stack
    open_account, write_receipt = _register_tools()

    with agent_txn(adapters, audit=audit, client_id="agent-A") as ctx:
        open_account(account_id=1, balance=100)
        write_receipt(path="receipts/1.txt", body="opened account 1")
        txn_id = ctx.txn_id

    # Both resources persisted through a single transaction.
    assert _balances(db_path) == [(1, 100)]
    assert (fs_root / "receipts/1.txt").read_text() == "opened account 1"

    # The audit journal tells the whole story: COMMITTED, one row per effect,
    # both attributed to the client_id, both APPLIED, spanning both resources.
    txn_row = audit.get_transaction(txn_id)
    assert txn_row["state"] == TxnState.COMMITTED.name
    assert txn_row["client_id"] == "agent-A"
    effects = audit.get_effects(txn_id)
    assert [e["tool"] for e in effects] == ["open_account", "write_receipt"]
    assert {e["resource"] for e in effects} == {"sql", "fs"}
    assert all(e["status"] == EffectStatus.APPLIED.name for e in effects)


def test_mid_body_exception_rolls_back_both_adapters(stack):
    adapters, audit, db_path, fs_root = stack
    open_account, write_receipt = _register_tools()

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with agent_txn(adapters, audit=audit, client_id="agent-A") as ctx:
            open_account(account_id=1, balance=100)
            write_receipt(path="receipts/1.txt", body="opened account 1")
            txn_id = ctx.txn_id
            raise Boom("agent crashed after writing both resources")

    # The backward fold is resource-blind: SQL row gone AND the file restored
    # (here: never existed, so removed) in one unwind.
    assert _balances(db_path) == []
    assert not (fs_root / "receipts/1.txt").exists()

    txn_row = audit.get_transaction(txn_id)
    assert txn_row["state"] == TxnState.ROLLED_BACK.name
    effects = audit.get_effects(txn_id)
    # Both reversible effects were applied then compensated by the unwind.
    assert all(e["status"] == EffectStatus.COMPENSATED.name for e in effects)


def test_commit_time_policy_cap_unwinds_everything(stack):
    adapters, audit, db_path, fs_root = stack
    open_account, write_receipt = _register_tools()

    # A spend cap: at most 2 SQL opens per txn. The third open trips the cap at
    # the commit-time re-walk; the whole transaction (sql + fs) unwinds.
    policy = Policy.with_rules(caps=[Cap.count(tool="open_account", max=2)])

    from pherix.core.policy import PolicyViolation

    with pytest.raises(PolicyViolation) as excinfo:
        with agent_txn(adapters, policy=policy, audit=audit) as ctx:
            open_account(account_id=1, balance=100)
            open_account(account_id=2, balance=200)
            open_account(account_id=3, balance=300)  # trips the cap at stage
            write_receipt(path="receipts/batch.txt", body="three accounts")
            txn_id = ctx.txn_id

    # Stage-time deny: the third open never journalled, so nothing about it
    # persisted, and the txn rolled back when the with-block propagated.
    assert excinfo.value.tool == "open_account"
    assert _balances(db_path) == []
    assert not (fs_root / "receipts/batch.txt").exists()


def test_stage_time_rule_denial_leaves_clean_audit_trail(stack):
    """A content-aware rule denies one effect at stage-time; the rest unwind.

    Exercises the full ``agent_txn -> policy -> adapters -> audit`` path for a
    *rule* (not a cap): the agent opens a fine account + a file, then tries a
    too-rich account. The rule denies it at stage-time, so it never journals;
    when the with-block propagates the PolicyViolation, the prior two effects
    roll back across both adapters. The audit shows ROLLED_BACK and carries no
    row for the denied effect (a stage-time deny journals nothing).
    """
    adapters, audit, db_path, fs_root = stack
    open_account, write_receipt = _register_tools()

    policy = Policy.allow_all()

    @policy.rule
    def cap_balance(effect, ctx):
        if effect.tool == "open_account" and effect.args.get("balance", 0) > 500:
            return Deny("balance exceeds the 500 ceiling")
        return Allow()

    from pherix.core.policy import PolicyViolation

    with pytest.raises(PolicyViolation) as excinfo:
        with agent_txn(adapters, policy=policy, audit=audit) as ctx:
            txn_id = ctx.txn_id
            open_account(account_id=1, balance=100)
            write_receipt(path="r.txt", body="ok")
            open_account(account_id=2, balance=900)  # denied at stage-time

    assert excinfo.value.where == "stage"
    assert excinfo.value.tool == "open_account"
    # Everything that DID journal unwound — neither resource changed.
    assert _balances(db_path) == []
    assert not (fs_root / "r.txt").exists()
    txn_row = audit.get_transaction(txn_id)
    assert txn_row["state"] == TxnState.ROLLED_BACK.name
    # Only the two accepted effects ever journalled; the denied one left no row.
    effects = audit.get_effects(txn_id)
    assert [e["tool"] for e in effects] == ["open_account", "write_receipt"]
    assert all(e["status"] == EffectStatus.COMPENSATED.name for e in effects)


def test_dry_run_state_diff_spans_both_adapters(stack):
    adapters, audit, db_path, fs_root = stack
    open_account, write_receipt = _register_tools()

    with dry_run(adapters, audit=audit, client_id="planner") as ctx:
        open_account(account_id=7, balance=700)
        write_receipt(path="plan.txt", body="planned account 7")

    result = ctx.result
    assert result.is_clean is True
    # The structural diff carries BOTH resources, each in its own contract shape.
    assert set(result.state_diff) == {"sql", "fs"}
    sql_diff = result.state_diff["sql"]
    assert len(sql_diff["rows_added"]) == 1
    assert sql_diff["rows_added"][0]["table"] == "accounts"
    fs_diff = result.state_diff["fs"]
    assert "plan.txt" in fs_diff["files_added"]

    # Nothing persisted — the world is bit-identical bar the dry_run audit row.
    assert _balances(db_path) == []
    assert not (fs_root / "plan.txt").exists()
    txn_row = audit.get_transaction(result.txn_id)
    assert txn_row["dry_run"] == 1
    assert txn_row["client_id"] == "planner"
    assert txn_row["state"] == TxnState.ROLLED_BACK.name


def test_isolation_conflict_across_two_connections(stack):
    """Two transactions racing the same row: the second commit aborts.

    Exercises the full isolation path end-to-end — read/write-key recording via
    ``execute_isolated``, the commit-time version diff, and the default
    :class:`Abort` resolution — over real on-disk SQLite (so the version
    side-table is genuinely shared, not a single in-memory connection).
    """
    adapters, audit, db_path, fs_root = stack
    open_account, _ = _register_tools()

    @tool(resource="sql")
    def read_balance(conn, account_id):
        """Read an account balance, recording the read for isolation."""
        row = execute_isolated(
            conn,
            "SELECT balance FROM accounts WHERE id = ?",
            (account_id,),
            reads=[("accounts", account_id)],
        ).fetchone()
        return row[0] if row else 0

    # Seed a row to contend over.
    with agent_txn(adapters, audit=audit) as ctx:
        open_account(account_id=1, balance=100)

    @tool(resource="sql", name="set_balance")
    def set_balance(conn, account_id, balance):
        """Overwrite an account balance (the contender's write)."""
        execute_isolated(
            conn,
            "UPDATE accounts SET balance = ? WHERE id = ?",
            (balance, account_id),
            writes=[("accounts", account_id)],
        )

    # A second adapter/connection to the SAME on-disk file — the contender.
    # WAL mode (set in the fixture) lets B write while A holds its txn open.
    conn2 = sqlite3.connect(db_path, isolation_level=None)
    sql2 = SQLiteAdapter(conn2)
    try:
        from pherix.core.isolation import IsolationConflict

        # A reads row 1 at version v0 (no write — so A holds no page lock),
        # then B writes + commits row 1 (bumping the shared version to v1 on
        # the on-disk side-table). A's auto-commit diff re-reads the version
        # via its meta-connection, sees the move, and aborts — the classic
        # lost-update on a value A had already read, caught.
        with pytest.raises(IsolationConflict) as info:
            with agent_txn(adapters, audit=audit, client_id="A") as ctx_a:
                assert read_balance(account_id=1) == 100  # A reads row 1 @ v0
                with agent_txn({"sql": sql2}, audit=audit, client_id="B"):
                    set_balance(account_id=1, balance=999)  # B bumps the version
                # A commits on with-exit → conflict diff fires → Abort.
        conflict = info.value.conflicts[0]
        assert conflict.resource == "sql"
        assert conflict.key == ("accounts", 1)
        # B's write survived; A rolled back cleanly.
        assert ctx_a.txn.state is TxnState.ROLLED_BACK
        assert _balances(db_path) == [(1, 999)]
    finally:
        conn2.close()
