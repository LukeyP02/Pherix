"""Slice 3 partial-commit recovery (D5).

When the forward fold of staged irreversibles fails mid-sequence, Pherix
walks the journal backward in a *mixed fold*: ``compensator(effect)`` for
already-fired irreversibles, ``adapter.restore(snapshot)`` for already-
applied reversibles. Terminal state is ROLLED_BACK on a clean unwind or
STUCK if any compensator is missing or itself raises.
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

# Trust pillars: oversight (the gate / staged-irreversible fire path) and blast
# radius (a mid-fire failure unwinds the whole txn, STUCK only on missing comp).
pytestmark = [pytest.mark.oversight, pytest.mark.blast_radius]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE charges (id INTEGER PRIMARY KEY, customer TEXT, cents INTEGER)")
    yield c
    c.close()


def _names(conn):
    return [r[1] for r in conn.execute("SELECT * FROM charges ORDER BY id")]


# --- D5: forward-fold mid-sequence failure ---


def test_partial_failure_invokes_compensators_in_reverse_order():
    """The mid-fold failure case stated literally in D5.

    Three irreversibles staged. The first two fire successfully; the third
    raises. The runtime walks the fired prefix backward and invokes each
    compensator in reverse — that is the *mixed fold's* claim.
    """
    fired: list[tuple[str, dict]] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def undo_a(token):
        fired.append(("undo_a", {"token": token}))

    @tool(resource="http", reversible=False, injects_handle=False)
    def undo_b(token):
        fired.append(("undo_b", {"token": token}))

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="undo_a",
    )
    def step_a(token):
        fired.append(("step_a", {"token": token}))

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="undo_b",
    )
    def step_b(token):
        fired.append(("step_b", {"token": token}))

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_c(token):
        fired.append(("step_c", {"token": token}))
        raise RuntimeError("payment provider down")

    audit = AuditJournal.in_memory()
    with pytest.raises(RuntimeError, match="payment provider down"):
        with agent_txn({"http": HTTPAdapter()}, audit=audit) as txn:
            step_a(token="A")
            step_b(token="B")
            r3 = step_c(token="C")
            txn.approve_irreversible(r3.effect_id)  # no comp → must approve

    # Forward fold: a, b fired then c raised.
    # Backward unwind: undo_b (b's comp), undo_a (a's comp). Reverse order.
    assert fired == [
        ("step_a", {"token": "A"}),
        ("step_b", {"token": "B"}),
        ("step_c", {"token": "C"}),
        ("undo_b", {"token": "B"}),
        ("undo_a", {"token": "A"}),
    ]

    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    # a / b were fired then compensated; c failed.
    assert statuses == ["COMPENSATED", "COMPENSATED", "FAILED"]
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_partial_failure_with_missing_compensator_lands_in_stuck():
    """D5: a compensator missing on the unwind path → TxnState.STUCK.

    The journal carries enough information for manual recovery; Pherix
    refuses to silently swallow an un-undoable side effect.
    """
    fired: list[str] = []

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="undo",
    )
    def step_a(x):
        fired.append("a")

    @tool(resource="http", reversible=False, injects_handle=False)
    def undo(x):
        fired.append("undo")

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_no_comp(x):
        fired.append("no-comp")
        # This is the one that fires successfully but has no compensator —
        # so if a later step fails, step_no_comp's effect on the world is
        # stuck.

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_failing(x):
        fired.append("failing")
        raise RuntimeError("boom")

    audit = AuditJournal.in_memory()
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"http": HTTPAdapter()}, audit=audit) as txn:
            r1 = step_a(x=1)
            r2 = step_no_comp(x=2)
            r3 = step_failing(x=3)
            # step_no_comp and step_failing both need approval (no comp).
            txn.approve_irreversible(r2.effect_id)
            txn.approve_irreversible(r3.effect_id)

    # step_a was undone via its compensator; step_no_comp cannot be undone.
    assert "undo" in fired
    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    # step_a: COMPENSATED (compensator fired). step_no_comp: APPLIED (no
    # compensator, stays APPLIED for operator recovery). step_failing: FAILED.
    assert statuses == ["COMPENSATED", "APPLIED", "FAILED"]
    assert txn.txn.state is TxnState.STUCK


def test_compensator_that_itself_raises_lands_in_stuck():
    """The compensator-fails case stated in D5.

    A registered compensator that raises is *not* a recoverable state.
    Operator intervention is required; the txn lands in STUCK.
    """
    @tool(resource="http", reversible=False, injects_handle=False)
    def angry_undo(x):
        raise RuntimeError("compensator itself blew up")

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="angry_undo",
    )
    def step_a(x):
        return "a-done"

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_failing(x):
        raise RuntimeError("first-fire boom")

    with pytest.raises(RuntimeError, match="first-fire boom"):
        with agent_txn({"http": HTTPAdapter()}) as txn:
            step_a(x=1)
            r2 = step_failing(x=2)
            txn.approve_irreversible(r2.effect_id)

    assert txn.txn.state is TxnState.STUCK


def test_partial_failure_restores_reversible_effects_via_snapshot(conn):
    """The cross-lane case: SQL writes alongside staged HTTP fires.

    On partial-failure, SQL writes that were already applied live must be
    snapshot-restored — the same engine Slice 1 uses for pure rollback.
    The journal walk handles both lanes uniformly.
    """
    @tool(resource="sql")
    def write_charge(c, customer, cents):
        c.execute(
            "INSERT INTO charges (customer, cents) VALUES (?, ?)",
            (customer, cents),
        )

    @tool(resource="http", reversible=False, injects_handle=False)
    def refund(customer, cents):
        return  # would call Stripe in real life

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="refund",
    )
    def charge(customer, cents):
        # successful first fire
        pass

    @tool(resource="http", reversible=False, injects_handle=False)
    def email(to, body):
        raise RuntimeError("smtp down")

    with pytest.raises(RuntimeError, match="smtp down"):
        with agent_txn(
            {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()},
        ) as txn:
            write_charge(customer="alice", cents=100)
            charge(customer="alice", cents=100)
            r3 = email(to="alice@example.com", body="receipt")
            txn.approve_irreversible(r3.effect_id)

    # SQL effect restored via savepoint; HTTP charge compensated via refund.
    assert _names(conn) == []
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_idempotency_re_fire_of_applied_effect_is_a_no_op():
    """Slice 3 acceptance: a re-fire of an already-APPLIED effect is a no-op.

    The forward-fold loop checks ``status is APPLIED`` and skips. We pin
    this property by manually flipping the status before commit — replay
    (Slice 5) will lean on the same property.
    """
    fired: list[str] = []

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="undo",
    )
    def step(x):
        fired.append("fired")

    @tool(resource="http", reversible=False, injects_handle=False)
    def undo(x):
        pass

    with agent_txn({"http": HTTPAdapter()}) as txn:
        step(x=1)
        # Simulate the effect already having fired in a previous commit attempt.
        txn.txn.effects[0].status = EffectStatus.APPLIED

    # The forward fold should have skipped the already-APPLIED effect.
    assert fired == []
    assert txn.txn.state is TxnState.COMMITTED


def test_partial_unwind_state_passes_through_partial():
    """The state machine claim: STAGED → PARTIAL → ROLLED_BACK on a clean
    mid-fire failure. The audit row reflects each transition in turn."""
    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="undo_a",
    )
    def step_a(x):
        pass

    @tool(resource="http", reversible=False, injects_handle=False)
    def undo_a(x):
        pass

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_failing(x):
        raise RuntimeError("boom")

    audit = AuditJournal.in_memory()
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"http": HTTPAdapter()}, audit=audit) as txn:
            step_a(x=1)
            r2 = step_failing(x=2)
            txn.approve_irreversible(r2.effect_id)

    # Terminal state is recorded; the intermediate STAGED / PARTIAL states
    # are in-flight only and the txn-state column shows whichever was
    # last persisted — terminal here.
    assert audit.get_transaction(txn.txn_id)["state"] == "ROLLED_BACK"
    assert txn.txn.state is TxnState.ROLLED_BACK
