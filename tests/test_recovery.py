"""Crash-consistent recovery (#9) — resume an interrupted backward fold.

The scenario these tests simulate is a *process death* part-way through (or
before) the unwind: a durable journal left with a non-terminal transaction whose
APPLIED irreversible effect still stands in the world, the in-memory
``Transaction`` gone with the dead process. A *fresh* process — modelled here as
fresh adapters, a fresh registry, and a new connection pointed at the same
on-disk DB — runs :func:`recover` and resumes the fold.

These tests would FAIL against main @ 66b3204: ``pherix.core.recovery`` does not
exist there. ``STUCK`` is terminal on main and nothing resumes an interrupted
unwind cross-process.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.recovery import recover
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY, tool
from pherix.core.transaction import Transaction, TxnState


# --- a fake irreversible adapter whose compensator fires count is observable --


class CountingAdapter:
    """A fake irreversible adapter — the recovery target.

    Stands in for an HTTP/external adapter: it cannot roll back, so its
    side effects are undone by re-firing a compensator. The compensator
    is a plain registered tool; we count how many times the adapter
    actually applied it, which is what proves exactly-once.
    """

    name = "ext"

    def __init__(self) -> None:
        self.applied: list[tuple[str, dict]] = []

    def supports_rollback(self) -> bool:
        return False

    def apply(self, effect: Effect, tool_fn):  # type: ignore[no-untyped-def]
        self.applied.append((effect.tool, dict(effect.args)))
        return tool_fn(**effect.args)


def _persist_effect(audit: AuditJournal, effect: Effect) -> None:
    audit.record_effect(effect)
    audit.update_effect(effect)


def _seed_interrupted_unwind(db_path: str, *, applied_irreversible_tool: str) -> str:
    """Hand-build a durable journal that looks like a crash mid-unwind.

    A PARTIAL transaction with one APPLIED irreversible effect (its real-world
    side effect stands; its compensator never ran) plus one already-COMPENSATED
    irreversible (a step the in-process unwind reversed *before* the crash —
    the idempotency fence must leave it alone).

    Returns the txn_id.
    """
    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = TxnState.PARTIAL
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, TxnState.PARTIAL.name)

    # index 0: an irreversible the pre-crash unwind already compensated.
    already = Effect(
        txn_id=txn.txn_id,
        index=0,
        tool="charge_setup_fee",
        args={"amount": 5},
        resource="ext",
        reversible=False,
        status=EffectStatus.COMPENSATED,
    )
    _persist_effect(audit, already)

    # index 1: the APPLIED irreversible whose compensator never fired — the
    # standing side effect recovery must reverse exactly once.
    standing = Effect(
        txn_id=txn.txn_id,
        index=1,
        tool=applied_irreversible_tool,
        args={"amount": 100},
        resource="ext",
        reversible=False,
        status=EffectStatus.APPLIED,
    )
    _persist_effect(audit, standing)

    audit.close()
    return txn.txn_id


def test_recovery_resumes_interrupted_unwind_and_fires_compensator_once(tmp_path):
    """The core claim: a crash mid-unwind leaves an APPLIED irreversible; a
    fresh process resumes the fold, fires the compensator exactly once, and the
    txn lands ROLLED_BACK."""

    @tool(resource="ext", reversible=False, injects_handle=False)
    def refund(amount):
        pass

    @tool(
        resource="ext", reversible=False, injects_handle=False,
        compensator="refund",
    )
    def charge(amount):
        pass

    db_path = str(tmp_path / "journal.db")
    txn_id = _seed_interrupted_unwind(db_path, applied_irreversible_tool="charge")

    # --- simulate the new process: fresh adapter, registry already holds the
    # tools (they're module-level @tool defs), new connection to the same DB ---
    adapter = CountingAdapter()
    report = recover(db_path, {"ext": adapter})

    # The compensator (refund) fired exactly once, for the standing charge only.
    assert adapter.applied == [("refund", {"amount": 100})]

    # The txn landed terminal: ROLLED_BACK.
    assert len(report.transactions) == 1
    tr = report.transactions[0]
    assert tr.txn_id == txn_id
    assert tr.final_state == TxnState.ROLLED_BACK.name
    assert tr.compensators_fired == 1

    # Durable journal reflects it: both irreversibles now COMPENSATED, txn
    # ROLLED_BACK.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    statuses = [
        r["status"]
        for r in conn.execute(
            "SELECT status FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        )
    ]
    state = conn.execute(
        "SELECT state FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()["state"]
    conn.close()
    assert statuses == ["COMPENSATED", "COMPENSATED"]
    assert state == "ROLLED_BACK"


def test_recovery_is_idempotent_second_pass_is_a_no_op(tmp_path):
    """Exactly-once across recovery passes: re-running recover against the same
    DB fires no compensator the second time. The durable status is the fence."""

    @tool(resource="ext", reversible=False, injects_handle=False)
    def refund(amount):
        pass

    @tool(
        resource="ext", reversible=False, injects_handle=False,
        compensator="refund",
    )
    def charge(amount):
        pass

    db_path = str(tmp_path / "journal.db")
    _seed_interrupted_unwind(db_path, applied_irreversible_tool="charge")

    first = CountingAdapter()
    recover(db_path, {"ext": first})
    assert len(first.applied) == 1  # fired once on the first pass

    # Second pass — a fresh adapter so its counter starts clean. The txn is now
    # terminal (ROLLED_BACK) with all effects COMPENSATED, so nothing should
    # fire.
    second = CountingAdapter()
    report = recover(db_path, {"ext": second})
    assert second.applied == []  # exactly-once: no re-fire
    assert report.transactions == []  # no longer mid-flight


def test_recovery_via_audit_journal_object(tmp_path):
    """recover() accepts a live AuditJournal too, not only a path — used when
    recovery runs in the same process that holds the journal."""

    @tool(resource="ext", reversible=False, injects_handle=False)
    def refund(amount):
        pass

    @tool(
        resource="ext", reversible=False, injects_handle=False,
        compensator="refund",
    )
    def charge(amount):
        pass

    db_path = str(tmp_path / "journal.db")
    txn_id = _seed_interrupted_unwind(db_path, applied_irreversible_tool="charge")

    # Open the journal as an object and hand it straight to recover().
    audit = AuditJournal(db_path)
    adapter = CountingAdapter()
    report = recover(audit, {"ext": adapter})
    assert adapter.applied == [("refund", {"amount": 100})]
    assert report.transactions[0].final_state == TxnState.ROLLED_BACK.name
    # The connection we passed must NOT have been closed by recover().
    assert audit.get_transaction(txn_id)["state"] == "ROLLED_BACK"
    audit.close()


def test_recovery_lands_stuck_when_compensator_missing(tmp_path):
    """An APPLIED irreversible whose tool declares no compensator cannot be
    undone — recovery lands the txn STUCK and leaves the effect APPLIED for
    manual recovery, honestly."""

    @tool(resource="ext", reversible=False, injects_handle=False)
    def send_email(amount):
        pass  # no compensator declared

    db_path = str(tmp_path / "journal.db")
    txn_id = _seed_interrupted_unwind(db_path, applied_irreversible_tool="send_email")

    adapter = CountingAdapter()
    report = recover(db_path, {"ext": adapter})

    assert adapter.applied == []  # nothing to fire
    tr = report.transactions[0]
    assert tr.final_state == TxnState.STUCK.name
    assert any(e.action == "stuck_missing_compensator" for e in tr.effects)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The standing effect stays APPLIED (operator's recovery target); the
    # pre-compensated one stays COMPENSATED.
    statuses = [
        r["status"]
        for r in conn.execute(
            "SELECT status FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        )
    ]
    state = conn.execute(
        "SELECT state FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()["state"]
    conn.close()
    assert statuses == ["COMPENSATED", "APPLIED"]
    assert state == "STUCK"


def test_recovery_lands_stuck_when_compensator_raises(tmp_path):
    """A registered compensator that itself raises is not a recoverable state:
    the inverse did not apply, so the effect stays APPLIED and the txn STUCK."""

    @tool(resource="ext", reversible=False, injects_handle=False)
    def angry_refund(amount):
        raise RuntimeError("provider rejected the refund")

    @tool(
        resource="ext", reversible=False, injects_handle=False,
        compensator="angry_refund",
    )
    def charge(amount):
        pass

    db_path = str(tmp_path / "journal.db")
    txn_id = _seed_interrupted_unwind(db_path, applied_irreversible_tool="charge")

    adapter = CountingAdapter()
    report = recover(db_path, {"ext": adapter})

    tr = report.transactions[0]
    assert tr.final_state == TxnState.STUCK.name
    assert any(e.action == "stuck_compensator_raised" for e in tr.effects)

    # The effect stays APPLIED — its inverse never applied.
    conn = sqlite3.connect(db_path)
    statuses = [
        r[0]
        for r in conn.execute(
            "SELECT status FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        )
    ]
    conn.close()
    assert statuses == ["COMPENSATED", "APPLIED"]


def test_reversible_applied_effect_is_db_auto_rolled_back(tmp_path):
    """A reversible (SQL) effect left APPLIED-but-uncommitted at the crash is
    already undone by the DB on connection death — recovery records the fact
    and does NOT attempt a dead-savepoint restore."""
    db_path = str(tmp_path / "journal.db")

    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = TxnState.PARTIAL
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, TxnState.PARTIAL.name)

    sql_effect = Effect(
        txn_id=txn.txn_id,
        index=0,
        tool="insert_row",
        args={"name": "alice"},
        resource="sql",
        reversible=True,
        status=EffectStatus.APPLIED,
    )
    audit.record_effect(sql_effect)
    audit.update_effect(sql_effect)
    txn_id = txn.txn_id
    audit.close()

    # No adapter call should be needed for the reversible — but recovery still
    # needs an entry for "sql" if it tried to resolve one. It must NOT.
    report = recover(db_path, {})  # empty adapter map: proves no restore call

    tr = report.transactions[0]
    assert tr.final_state == TxnState.ROLLED_BACK.name
    assert [e.action for e in tr.effects] == ["db_auto_rolled_back"]

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT status FROM effects WHERE txn_id = ? AND idx = 0", (txn_id,)
    ).fetchone()[0]
    conn.close()
    assert status == "COMPENSATED"


def test_committed_txn_is_not_touched(tmp_path):
    """A cleanly COMMITTED transaction is terminal and is never a recovery
    candidate, even though it has APPLIED effects."""
    db_path = str(tmp_path / "journal.db")

    audit = AuditJournal(db_path)
    txn = Transaction()
    audit.record_transaction(txn)
    e = Effect(
        txn_id=txn.txn_id, index=0, tool="charge", args={"amount": 1},
        resource="ext", reversible=False, status=EffectStatus.APPLIED,
    )
    audit.record_effect(e)
    audit.update_effect(e)
    audit.update_transaction_state(txn.txn_id, TxnState.COMMITTED.name)
    audit.close()

    adapter = CountingAdapter()
    report = recover(db_path, {"ext": adapter})
    assert report.transactions == []
    assert adapter.applied == []


def test_real_agent_txn_crash_then_recover(tmp_path):
    """End-to-end via a real agent_txn: a STUCK landing leaves an APPLIED
    irreversible (the step with no compensator), then a fresh recover() pass
    sees that the no-compensator effect is genuinely unrecoverable and the
    compensator-backed effect was already reversed in-process.

    This proves the durable journal a real run leaves behind is exactly what
    recovery consumes — not just a hand-built fixture.
    """
    fired: list[str] = []

    @tool(resource="ext", reversible=False, injects_handle=False)
    def undo_first(x):
        fired.append("undo_first")

    @tool(
        resource="ext", reversible=False, injects_handle=False,
        compensator="undo_first",
    )
    def first(x):
        fired.append("first")

    @tool(resource="ext", reversible=False, injects_handle=False)
    def second_no_comp(x):
        fired.append("second")  # applies, but cannot be undone

    @tool(resource="ext", reversible=False, injects_handle=False)
    def third_fails(x):
        raise RuntimeError("boom mid-fire")

    db_path = str(tmp_path / "journal.db")
    audit = AuditJournal(db_path)

    with pytest.raises(RuntimeError, match="boom mid-fire"):
        with agent_txn({"ext": CountingAdapter()}, audit=audit) as txn:
            first(x=1)
            r2 = second_no_comp(x=2)
            r3 = third_fails(x=3)
            txn.approve_irreversible(r2.effect_id)
            txn.approve_irreversible(r3.effect_id)

    txn_id = txn.txn_id
    audit.close()

    # The in-process unwind left it STUCK: first compensated, second_no_comp
    # APPLIED (no compensator), third FAILED.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    pre = [
        r["status"]
        for r in conn.execute(
            "SELECT status FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        )
    ]
    conn.close()
    assert pre == ["COMPENSATED", "APPLIED", "FAILED"]

    # Fresh process recovers from the durable journal. The standing
    # second_no_comp is genuinely unrecoverable → still STUCK, no re-fire of
    # the already-COMPENSATED first.
    adapter = CountingAdapter()
    report = recover(db_path, {"ext": adapter})
    assert adapter.applied == []  # undo_first already ran in-process; not again
    tr = report.transactions[0]
    assert tr.final_state == TxnState.STUCK.name
    assert any(e.action == "stuck_missing_compensator" for e in tr.effects)
    # the already-compensated effect was left alone (the fence)
    assert any(e.action == "already_compensated" for e in tr.effects)
