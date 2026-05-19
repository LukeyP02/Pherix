"""Slice 3 commit-time gate (D3) and policy re-eval (D4)."""

from __future__ import annotations

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


def _make_charge_and_refund():
    """Charge + its compensator; both registered as @tools (D2)."""
    calls: list[tuple[str, dict]] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def refund_charge(customer_id, amount):
        calls.append(("refund_charge", {"customer_id": customer_id, "amount": amount}))

    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="refund_charge",
    )
    def charge_card(customer_id, amount):
        calls.append(("charge_card", {"customer_id": customer_id, "amount": amount}))
        return {"charge_id": "ch_xyz"}

    return charge_card, refund_charge, calls


def _make_send_email():
    """An irreversible tool with no compensator — needs approval to commit."""
    calls: list[dict] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, body):
        calls.append({"to": to, "body": body})

    return send_email, calls


# --- gate-block path ---


def test_staged_irreversible_without_comp_or_approval_blocks_commit():
    send_email, calls = _make_send_email()
    with pytest.raises(GateBlocked) as excinfo:
        with agent_txn({"http": HTTPAdapter()}):
            send_email(to="alice@example.com", body="hi")
    assert calls == []  # blocked at the gate; never fired
    # The exception lists the still-needed effect_ids — operators / agents
    # can use those to know exactly which staged effects to approve.
    assert len(excinfo.value.needs_approval) == 1


def test_gate_block_lists_all_needs_approval_effect_ids():
    s1, _ = _make_send_email()
    with pytest.raises(GateBlocked) as excinfo:
        with agent_txn({"http": HTTPAdapter()}) as txn:
            s1(to="a@example.com", body="x")
            s1(to="b@example.com", body="y")
            s1(to="c@example.com", body="z")
        # auto-commit on exit raises GateBlocked
    # All three effect_ids are reported, not just the first.
    assert len(excinfo.value.needs_approval) == 3


def test_gate_block_marks_effects_gated_and_txn_rolled_back():
    s, _ = _make_send_email()
    audit = AuditJournal.in_memory()
    with pytest.raises(GateBlocked):
        with agent_txn({"http": HTTPAdapter()}, audit=audit) as txn:
            s(to="a@example.com", body="x")
    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    assert statuses == ["GATED"]
    assert txn.txn.state is TxnState.ROLLED_BACK


# --- approval path ---


def test_approve_irreversible_lets_commit_pass():
    s, calls = _make_send_email()
    with agent_txn({"http": HTTPAdapter()}) as txn:
        result = s(to="alice@example.com", body="hi")
        txn.approve_irreversible(result.effect_id)
    # No exception raised — the email actually fired at commit.
    assert calls == [{"to": "alice@example.com", "body": "hi"}]
    assert txn.txn.state is TxnState.COMMITTED


def test_compensator_backed_effect_does_not_need_approval():
    # D3: the gate is satisfied by EITHER a registered compensator OR a
    # pre-approval. A compensator-backed effect can fire without anyone
    # calling approve_irreversible().
    charge, _, calls = _make_charge_and_refund()
    with agent_txn({"http": HTTPAdapter()}) as txn:
        charge(customer_id="c1", amount=100)
    assert ("charge_card", {"customer_id": "c1", "amount": 100}) in calls
    assert txn.txn.state is TxnState.COMMITTED


def test_approve_irreversible_unknown_effect_id_raises():
    s, _ = _make_send_email()
    with agent_txn({"http": HTTPAdapter()}) as txn:
        s(to="a@example.com", body="x")
        with pytest.raises(ValueError, match="no staged effect"):
            txn.approve_irreversible("not-a-real-effect-id")
        # Clear the gate so __exit__ doesn't compound the failure.
        txn.approve_irreversible(txn.txn.effects[0].effect_id)


def test_approval_is_per_effect_not_blanket():
    s, _ = _make_send_email()
    with pytest.raises(GateBlocked) as excinfo:
        with agent_txn({"http": HTTPAdapter()}) as txn:
            r1 = s(to="a@example.com", body="x")
            r2 = s(to="b@example.com", body="y")
            r3 = s(to="c@example.com", body="z")
            txn.approve_irreversible(r1.effect_id)
            txn.approve_irreversible(r3.effect_id)
            # r2 deliberately not approved.
    # Only r2 should still need approval.
    assert len(excinfo.value.needs_approval) == 1


# --- D4: commit-time policy re-eval ---


class _MutablePolicy(Policy):
    """A policy whose deny-set can grow between stage and commit.

    Models Slice 6's state-dependent policy with the minimum machinery:
    Slice 1's allow-list is stateless and re-eval is trivially equal at
    both points; the hook still has to live in the right place.
    """

    def revoke(self, tool_name: str) -> None:
        self.deny.add(tool_name)


def test_policy_revoked_between_stage_and_commit_blocks_irreversible():
    # The classic TOCTOU: tool was allowed at stage-time, but world-state
    # changed (here: the policy deny-set grew) before commit. D4 says
    # policy.check runs *again* at commit start for each staged effect;
    # this test pins that the second evaluation actually rules.
    charge, _, calls = _make_charge_and_refund()
    policy = _MutablePolicy()
    with pytest.raises(PolicyViolation, match="charge_card"):
        with agent_txn({"http": HTTPAdapter()}, policy=policy) as txn:
            charge(customer_id="c1", amount=100)
            policy.revoke("charge_card")
    assert calls == []  # never fired despite passing stage-time policy


def test_policy_recheck_marks_effect_gated_and_rolls_back():
    charge, _, _ = _make_charge_and_refund()
    policy = _MutablePolicy()
    audit = AuditJournal.in_memory()
    with pytest.raises(PolicyViolation):
        with agent_txn({"http": HTTPAdapter()}, policy=policy, audit=audit) as txn:
            charge(customer_id="c1", amount=100)
            policy.revoke("charge_card")
    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    assert statuses == ["GATED"]
    assert txn.txn.state is TxnState.ROLLED_BACK
