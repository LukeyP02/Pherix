"""Payment compensators tested as true left-inverses.

For each pair, with a fake in-memory client that tracks state:
  1. left-inverse / golden — action fires, then unwind, state back to baseline
  2. partial-failure — A + B fire, C raises, BOTH compensators fire
  3. commit (no rollback) — action fires once, compensator never fires
  4. adversarial — a compensator that raises lands the txn STUCK

CRITICAL engine fact these tests are built around: irreversible effects do
NOT fire at stage-time — they stage, and fire only during ``commit()``'s
forward fold. So a body that simply ``raise``s *before* commit leaves every
irreversible STAGED-and-never-fired: there is nothing to compensate, and a
"state == baseline" assertion would pass *vacuously*. To exercise a genuine
left-inverse the action must actually FIRE and then be unwound — which the
engine does when a *later* staged irreversible raises during the commit
forward fold (see ``tests/test_runtime_partial_commit.py``). So every
golden / partial test below registers a ``tripwire`` irreversible that fires
last and raises, driving the real ``fire → compensate`` path.

The fake clients track balances/ledgers in plain dicts so "state returned
to baseline" is a concrete equality assertion.
"""

from __future__ import annotations

import pytest

from pherix.compensators.payments import (
    register_charge_refund,
    register_payout_reverse,
)
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


def _tripwire():
    """A compensator-backed irreversible that fires last and raises, forcing
    the commit-time forward fold to unwind every prior fired irreversible."""

    @tool(resource="payments", reversible=False, injects_handle=False)
    def _tripwire_undo():
        pass

    @tool(
        resource="payments",
        reversible=False,
        injects_handle=False,
        compensator="_tripwire_undo",
    )
    def tripwire():
        raise RuntimeError("boom")

    return tripwire


class FakePaymentClient:
    """In-memory payment provider. ``captured`` maps idempotency_key ->
    net cents currently held (charge adds, refund zeroes)."""

    def __init__(self):
        self.captured: dict[str, int] = {}
        self.payouts: dict[str, int] = {}
        self.charge_calls = 0
        self.refund_calls = 0

    def charge(self, idempotency_key, amount_cents, currency):
        self.charge_calls += 1
        self.captured[idempotency_key] = amount_cents
        return {"id": idempotency_key, "status": "captured"}

    def refund(self, idempotency_key):
        self.refund_calls += 1
        self.captured.pop(idempotency_key, None)
        return {"id": idempotency_key, "status": "refunded"}

    def payout(self, payout_id, amount_cents, destination):
        self.payouts[payout_id] = amount_cents
        return {"id": payout_id, "status": "paid"}

    def reverse_payout(self, payout_id):
        self.payouts.pop(payout_id, None)
        return {"id": payout_id, "status": "reversed"}


# --- charge → refund ------------------------------------------------------


def test_charge_refund_left_inverse():
    client = FakePaymentClient()
    baseline = dict(client.captured)  # {}
    charge, _refund = register_charge_refund(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"payments": HTTPAdapter()}):
            charge(idempotency_key="ik_1", amount_cents=5000)
            tripwire()  # fires after the charge and raises → forces unwind

    # refund ∘ charge ≈ identity: the charge fired, then was refunded back
    # to baseline. charge_calls==1 proves the action genuinely fired (not a
    # vacuous never-staged pass); refund_calls==1 proves the inverse ran.
    assert client.captured == baseline
    assert client.charge_calls == 1
    assert client.refund_calls == 1


def test_charge_partial_failure_unwinds_both():
    client = FakePaymentClient()
    charge, _ = register_charge_refund(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"payments": HTTPAdapter()}):
            charge(idempotency_key="ik_a", amount_cents=100)
            charge(idempotency_key="ik_b", amount_cents=200)
            charge(idempotency_key="ik_c", amount_cents=300)
            tripwire()

    # All three charges fired at commit, then all three refunded on unwind.
    assert client.captured == {}
    assert client.charge_calls == 3
    assert client.refund_calls == 3


def test_charge_clean_commit_does_not_refund():
    client = FakePaymentClient()
    charge, _ = register_charge_refund(client)

    with agent_txn({"payments": HTTPAdapter()}) as txn:
        charge(idempotency_key="ik_1", amount_cents=5000)

    assert txn.txn.state is TxnState.COMMITTED
    assert client.captured == {"ik_1": 5000}
    assert client.charge_calls == 1
    assert client.refund_calls == 0  # compensator never fires on clean commit


def test_charge_refund_that_raises_lands_stuck():
    """Adversarial: the refund itself fails on the unwind path → STUCK."""

    class BrokenRefundClient(FakePaymentClient):
        def refund(self, idempotency_key):
            raise RuntimeError("refund endpoint 500")

    client = BrokenRefundClient()
    charge, _ = register_charge_refund(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"payments": HTTPAdapter()}) as txn:
            charge(idempotency_key="ik_1", amount_cents=5000)
            tripwire()

    # Compensator raised → the engine documents this as STUCK; assert it,
    # don't fight it. The charge stays recorded for manual recovery.
    assert txn.txn.state is TxnState.STUCK
    assert client.captured == {"ik_1": 5000}


# --- payout → reverse_payout ----------------------------------------------


def test_payout_reverse_left_inverse():
    client = FakePaymentClient()
    payout, _ = register_payout_reverse(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"payments": HTTPAdapter()}):
            payout(payout_id="po_1", amount_cents=9000, destination="acct_1")
            tripwire()

    assert client.payouts == {}


def test_payout_clean_commit():
    client = FakePaymentClient()
    payout, _ = register_payout_reverse(client)

    with agent_txn({"payments": HTTPAdapter()}) as txn:
        payout(payout_id="po_1", amount_cents=9000, destination="acct_1")

    assert txn.txn.state is TxnState.COMMITTED
    assert client.payouts == {"po_1": 9000}


def test_payments_cross_pair_partial_unwind():
    """charge then payout then a raise → both inverses fire, full restore."""
    client = FakePaymentClient()
    charge, _ = register_charge_refund(client)
    payout, _ = register_payout_reverse(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"payments": HTTPAdapter()}):
            charge(idempotency_key="ik_1", amount_cents=100)
            payout(payout_id="po_1", amount_cents=50, destination="acct_1")
            tripwire()

    assert client.captured == {}
    assert client.payouts == {}
