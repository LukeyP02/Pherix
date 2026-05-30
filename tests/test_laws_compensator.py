"""Property-based law of compensation: ``compensator ∘ tool ≈ identity``.

Compensators are the *other* undo mechanism — the semantic left-inverse the
runtime fires for an irreversible effect that has no snapshot to restore. The
law: after a mid-commit failure unwinds a sequence of fired irreversibles, the
external world returns to the committed baseline, and each fired effect is
compensated **exactly once** (no double-refund, no missed refund).

We model an external payment ledger behind an irreversible
:class:`~tests._laws.LedgerAdapter`. ``charge`` adds to a balance; its declared
compensator ``refund`` subtracts the *same* amount (the runtime fires the
compensator with the original effect's args). A trailing ``boom_charge`` whose
tool raises is the failure that triggers the backward fold, so the charges
ahead of it are the ones the law watches return to baseline.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pherix.core.effects import EffectStatus
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

from tests._laws import (
    LEDGER_ACCOUNTS,
    LedgerAdapter,
    charge_impl,
    charge_programs,
    ledger_equal,
    refund_impl,
)

# Trust pillar: blast radius — the compensator left-inverse law.
pytestmark = pytest.mark.blast_radius

_LAW = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def ledger_seeds():
    """Arbitrary starting balances — the baseline the unwind must restore to."""
    return st.dictionaries(
        keys=st.sampled_from(LEDGER_ACCOUNTS),
        values=st.integers(min_value=0, max_value=100_000),
        max_size=len(LEDGER_ACCOUNTS),
    )


@pytest.fixture
def ledger_tools():
    """The irreversible charge/refund catalog + a failing trigger, once/function."""

    @tool(resource="ledger", reversible=False, compensator="refund")
    def charge(ledger, account, amount):
        return charge_impl(ledger, account, amount)

    @tool(resource="ledger", reversible=False)
    def refund(ledger, account, amount):
        return refund_impl(ledger, account, amount)

    @tool(resource="ledger", reversible=False, compensator="boom_undo")
    def boom_charge(ledger, account, amount):
        # Fails *before* mutating, so it puts nothing in the world; it exists
        # only to trigger the backward fold over the charges ahead of it.
        raise RuntimeError("payment gateway timeout")

    @tool(resource="ledger", reversible=False)
    def boom_undo(ledger, account, amount):  # pragma: no cover - never fires
        # boom_charge never reaches APPLIED, so its compensator never runs;
        # it exists only so the commit-time gate is satisfied.
        return refund_impl(ledger, account, amount)

    return charge, boom_charge


@given(seed=ledger_seeds(), charges=charge_programs())
@_LAW
def test_partial_unwind_restores_ledger_to_baseline(ledger_tools, seed, charges):
    """For any sequence of fired charges, the backward fold of refunds returns
    the ledger to its committed baseline — left-inverse over the partial-
    failure path."""
    charge, boom_charge = ledger_tools
    ledger = dict(seed)
    before = dict(ledger)

    with pytest.raises(RuntimeError, match="timeout"):
        with agent_txn({"ledger": LedgerAdapter(ledger)}) as txn:
            for c in charges:
                charge(account=c.account, amount=c.amount)
            boom_charge(account="alice", amount=1)

    assert ledger_equal(ledger, before)
    # Every charge fired then was compensated; the txn lands terminal.
    assert txn.txn.state is TxnState.ROLLED_BACK
    charge_effects = [e for e in txn.txn.effects if e.tool == "charge"]
    assert len(charge_effects) == len(charges)
    assert all(e.status is EffectStatus.COMPENSATED for e in charge_effects)


@given(seed=ledger_seeds(), charges=charge_programs(min_size=1))
@_LAW
def test_each_compensator_fires_exactly_once(ledger_tools, seed, charges):
    """Net ledger change is zero — proving no charge was refunded twice and
    none was skipped. The per-account arithmetic is the exactly-once fence
    made observable: sum of charges minus sum of refunds = 0 per account."""
    charge, boom_charge = ledger_tools
    ledger = dict(seed)

    # What the ledger would read if every charge fired and nothing was undone.
    expected_if_no_unwind = dict(seed)
    for c in charges:
        expected_if_no_unwind[c.account] = (
            expected_if_no_unwind.get(c.account, 0) + c.amount
        )

    with pytest.raises(RuntimeError, match="timeout"):
        with agent_txn({"ledger": LedgerAdapter(ledger)}) as txn:
            for c in charges:
                charge(account=c.account, amount=c.amount)
            boom_charge(account="alice", amount=1)

    # If a compensator had fired twice the balance would dip below baseline;
    # if one had been skipped it would sit above. Exactly-once ⇒ baseline.
    assert ledger_equal(ledger, seed)
    # The charges genuinely moved the ledger before the unwind (otherwise the
    # exactly-once claim would be vacuous): the no-unwind world differs.
    assert not ledger_equal(seed, expected_if_no_unwind)
