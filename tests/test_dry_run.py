"""Slice 7 — speculative dry-run end-to-end.

Pins the four contracts the slice ships:

  1. World-unchanged after exit (the load-bearing pin).
  2. Journal is the per-effect record exactly as agent_txn would produce.
  3. would_have_fired filters the journal to staged irreversibles.
  4. policy_verdicts capture stage-time AND commit-time, without raising.

Plus the audit dry_run column round-trip and the cross-resource case.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from pherix import (
    Allow,
    AuditJournal,
    Cap,
    Deny,
    DryRunResult,
    FilesystemAdapter,
    HTTPAdapter,
    Policy,
    PolicyVerdict,
    SQLiteAdapter,
    StagedResult,
    agent_txn,
    dry_run,
    tool,
)
from pherix.core.adapters.filesystem import FsHandle
from pherix.core.effects import EffectStatus
from pherix.core.transaction import TxnState


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    yield c
    c.close()


@pytest.fixture
def fs_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    return root


def _note_rows(conn):
    return [tuple(r) for r in conn.execute("SELECT id, body FROM notes ORDER BY id")]


def _fs_snapshot(root: Path) -> dict[str, str]:
    """Content-addressed snapshot of every file under ``root``."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --- 1. world-unchanged property -------------------------------------------


def test_dry_run_leaves_sql_and_fs_byte_identical(conn, fs_root: Path):
    # Pre-state: one note, one file. The dry-run will try to add to both.
    conn.execute("INSERT INTO notes (body) VALUES ('pre-existing')")
    (fs_root / "before.txt").write_bytes(b"original")

    pre_rows = _note_rows(conn)
    pre_fs = _fs_snapshot(fs_root)

    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)

    notify_fires: list[str] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def notify(channel):
        # If the dry-run ever lets this fire, the list grows. Pin: it doesn't.
        notify_fires.append(channel)

    with dry_run(
        {
            "sql": SQLiteAdapter(conn),
            "fs": FilesystemAdapter(fs_root),
            "http": HTTPAdapter(),
        }
    ) as ctx:
        insert_note(body="dry-1")
        write_file(path="new.txt", data=b"would-be-content")
        insert_note(body="dry-2")
        notify(channel="ops")

    # The load-bearing pin: world is bit-identical to its pre-dry-run state.
    assert _note_rows(conn) == pre_rows
    assert _fs_snapshot(fs_root) == pre_fs
    # The irreversible function body never ran — Slice 3 staging held the line.
    assert notify_fires == []


# --- 2. journal materialises in full ---------------------------------------


def test_journal_records_every_effect_with_real_metadata(conn, fs_root: Path):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)

    @tool(resource="http", reversible=False, injects_handle=False)
    def notify(channel):
        return "notified"

    with dry_run(
        {
            "sql": SQLiteAdapter(conn),
            "fs": FilesystemAdapter(fs_root),
            "http": HTTPAdapter(),
        }
    ) as ctx:
        insert_note(body="x")
        write_file(path="a.txt", data=b"a")
        notify(channel="ops")

    result: DryRunResult = ctx.result
    assert result is not None
    assert len(result.journal) == 3
    assert [(e.tool, e.resource) for e in result.journal] == [
        ("insert_note", "sql"),
        ("write_file", "fs"),
        ("notify", "http"),
    ]
    # Reversibles ran, then unwound — final status COMPENSATED (snapshot restored).
    assert result.journal[0].status is EffectStatus.COMPENSATED
    assert result.journal[1].status is EffectStatus.COMPENSATED
    # Irreversible stayed STAGED — never fired.
    assert result.journal[2].status is EffectStatus.STAGED


# --- 3. would_have_fired filter --------------------------------------------


def test_would_have_fired_filters_journal_to_staged_irreversibles(conn, fs_root: Path):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    @tool(resource="http", reversible=False, injects_handle=False)
    def charge(customer_id, amount):
        return {"charge_id": f"ch_{customer_id}"}

    @tool(resource="http", reversible=False, injects_handle=False)
    def notify(channel):
        return "n"

    with dry_run(
        {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    ) as ctx:
        insert_note(body="x")
        charge(customer_id="c1", amount=20)
        insert_note(body="y")
        notify(channel="ops")

    fired = ctx.result.would_have_fired
    assert len(fired) == 2
    assert {e.tool for e in fired} == {"charge", "notify"}
    # Reversibles excluded.
    assert all((not e.reversible) for e in fired)
    assert all(e.status is EffectStatus.STAGED for e in fired)


def test_staged_irreversible_returns_staged_result_sentinel_in_dry_run(conn):
    @tool(resource="http", reversible=False, injects_handle=False)
    def notify(channel):
        return "this should never run"

    with dry_run({"http": HTTPAdapter()}) as ctx:
        ret = notify(channel="ops")

    assert isinstance(ret, StagedResult)


# --- 4. policy verdicts captured (stage + commit) --------------------------


def test_clean_journal_produces_only_allow_verdicts(conn):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    policy = Policy.allow_all()

    @policy.rule
    def allow_everything(effect, ctx):
        return Allow()

    with dry_run({"sql": SQLiteAdapter(conn)}, policy=policy) as ctx:
        insert_note(body="a")
        insert_note(body="b")

    verdicts = ctx.result.policy_verdicts
    assert all(v.allow for v in verdicts)
    assert ctx.result.is_clean is True
    # 2 effects × (1 rule) × 2 evaluation points (stage + commit) = 4 verdicts.
    assert sum(1 for v in verdicts if v.rule_name == "allow_everything") == 4


def test_deny_during_body_does_not_abort_with_block(conn):
    """The D4 stage-time-capture pin: a Deny doesn't raise; the journal
    keeps growing; the result records the verdict."""

    @tool(resource="sql")
    def update_user(conn, user_id, tier):
        conn.execute(
            "INSERT INTO notes (body) VALUES (?)",
            (f"user-{user_id}-{tier}",),
        )

    policy = Policy.allow_all()

    @policy.rule
    def no_enterprise(effect, ctx):
        if effect.args.get("tier") == "enterprise":
            return Deny("enterprise off-limits")
        return Allow()

    with dry_run({"sql": SQLiteAdapter(conn)}, policy=policy) as ctx:
        update_user(user_id=1, tier="basic")
        update_user(user_id=2, tier="enterprise")  # would normally raise
        update_user(user_id=3, tier="basic")

    # All three landed in the journal.
    assert len(ctx.result.journal) == 3
    # is_clean reflects the Deny.
    assert ctx.result.is_clean is False
    # Find the Deny verdict for effect index 1 at stage-time.
    denies = [v for v in ctx.result.policy_verdicts if not v.allow]
    assert len(denies) >= 1
    enterprise_denies = [
        v for v in denies
        if v.rule_name == "no_enterprise" and v.effect_index == 1
    ]
    assert len(enterprise_denies) >= 1
    sample = enterprise_denies[0]
    assert sample.reason == "enterprise off-limits"
    assert sample.tool == "update_user"


def test_cap_sum_captured_at_stage_time_does_not_abort(conn):
    """A cap that would normally raise at stage-time captures instead in dry-run."""

    @tool(resource="http", reversible=False, injects_handle=False)
    def charge(customer_id, amount):
        return "ch"

    policy = Policy.with_rules(
        caps=[Cap.sum(tool="charge", via=lambda a: a["amount"], max=50)]
    )

    with dry_run({"http": HTTPAdapter()}, policy=policy) as ctx:
        charge(customer_id="c1", amount=20)  # running=20  allow
        charge(customer_id="c1", amount=25)  # running=45  allow
        charge(customer_id="c1", amount=10)  # would push to 55  deny

    assert len(ctx.result.journal) == 3
    assert ctx.result.is_clean is False
    cap_denies = [
        v for v in ctx.result.policy_verdicts
        if (not v.allow) and v.rule_name and v.rule_name.startswith("Cap.sum")
    ]
    # Stage-time + commit-time both flag the third charge.
    assert len(cap_denies) == 2
    assert all(v.effect_index == 2 for v in cap_denies)
    assert all(v.tool == "charge" for v in cap_denies)


def test_verdicts_carry_where_stage_then_commit(conn):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    policy = Policy.allow_all()

    @policy.rule
    def trace(effect, ctx):
        return Allow()

    with dry_run({"sql": SQLiteAdapter(conn)}, policy=policy) as ctx:
        insert_note(body="x")
        insert_note(body="y")

    wheres = [v.where for v in ctx.result.policy_verdicts]
    # Stage verdicts come first (in agent-body order), then commit verdicts.
    assert wheres.count("stage") == 2
    assert wheres.count("commit") == 2
    # Order: all stage entries precede all commit entries.
    last_stage = max(i for i, w in enumerate(wheres) if w == "stage")
    first_commit = min(i for i, w in enumerate(wheres) if w == "commit")
    assert last_stage < first_commit


# --- 5. audit dry_run column round-trips -----------------------------------


def test_audit_records_dry_run_flag(conn):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    audit = AuditJournal.in_memory()
    with dry_run({"sql": SQLiteAdapter(conn)}, audit=audit) as ctx:
        insert_note(body="x")
        txn_id = ctx.txn_id

    row = audit.get_transaction(txn_id)
    assert row["dry_run"] == 1
    assert row["state"] == TxnState.ROLLED_BACK.name


def test_audit_real_txns_keep_dry_run_zero(conn):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    audit = AuditJournal.in_memory()
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
        insert_note(body="x")
        txn_id = txn.txn_id

    row = audit.get_transaction(txn_id)
    assert row["dry_run"] == 0


# --- 6. body exception path -----------------------------------------------


def test_body_exception_rolls_back_and_leaves_result_unset(conn, fs_root: Path):
    """A genuine error in the body is not policy denial — it propagates,
    no DryRunResult materialises, and the world is still unchanged."""

    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    pre_rows = _note_rows(conn)

    with pytest.raises(RuntimeError, match="agent died"):
        with dry_run({"sql": SQLiteAdapter(conn)}) as ctx:
            insert_note(body="x")
            raise RuntimeError("agent died")

    assert _note_rows(conn) == pre_rows
    # Result not materialised — body raised before _dry_run_finalise.
    assert ctx.result is None
