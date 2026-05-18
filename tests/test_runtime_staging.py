"""Slice 3 staging lane — irreversible tools never fire at stage-time.

The agent calls a staged tool, gets a StagedResult sentinel, and the
journal records the effect with status STAGED. The actual fire is deferred
to commit(). Before commit, no side effect has reached the world.
"""

from __future__ import annotations

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus, StagedResult
from pherix.core.runtime import CompensatorNotRegistered, agent_txn
from pherix.core.tools import tool


def _fire_counter():
    calls: list[dict] = []

    @tool(resource="http", reversible=False, injects_handle=False, name="ping")
    def ping(url):
        calls.append({"url": url})
        return {"status": 200}

    return ping, calls


def test_staged_tool_returns_staged_result_sentinel():
    ping, calls = _fire_counter()
    with agent_txn({"http": HTTPAdapter()}) as txn:
        result = ping(url="https://example.com")
        assert isinstance(result, StagedResult)
        assert result.effect_id  # non-empty
        # Critical: the real HTTP call has NOT happened yet.
        assert calls == []
        # Approve so the gate passes and auto-commit fires the effect.
        txn.approve_irreversible(result.effect_id)

    # After commit, the call has fired exactly once.
    assert len(calls) == 1
    assert txn.txn.effects[0].status is EffectStatus.APPLIED


def test_staged_result_effect_id_matches_journal_entry():
    ping, _ = _fire_counter()
    with agent_txn({"http": HTTPAdapter()}) as txn:
        result = ping(url="https://example.com")
        assert isinstance(result, StagedResult)
        assert result.effect_id == txn.txn.effects[0].effect_id
        txn.approve_irreversible(result.effect_id)


def test_staged_effect_has_no_snapshot_and_no_live_result():
    # Asserted mid-txn — before commit — to pin the *staged* shape of the
    # effect: no snapshot, sentinel result, status STAGED. After commit
    # those flip to APPLIED with the tool's real return value.
    ping, _ = _fire_counter()
    with agent_txn({"http": HTTPAdapter()}) as txn:
        r = ping(url="https://example.com")
        e = txn.txn.effects[0]
        assert e.snapshot is None
        assert isinstance(e.result, StagedResult)
        assert e.status is EffectStatus.STAGED
        txn.approve_irreversible(r.effect_id)


def test_staged_effect_records_reversible_false_from_adapter():
    # The Effect's `reversible` flag is the adapter's verdict, not the
    # tool's optimistic `@tool(reversible=...)`. This isolates the staging
    # decision in one place: the adapter.
    @tool(resource="http", reversible=True, injects_handle=False)
    def lying_tool(url):
        return url

    with agent_txn({"http": HTTPAdapter()}) as txn:
        r = lying_tool(url="https://example.com")
        txn.approve_irreversible(r.effect_id)
    assert txn.txn.effects[0].reversible is False


def test_rollback_before_commit_means_staged_effects_never_fire():
    # The strongest containment property Pherix offers: irreversible
    # effects do not happen if the txn rolls back before commit.
    ping, calls = _fire_counter()
    audit = AuditJournal()
    with agent_txn({"http": HTTPAdapter()}, audit=audit) as txn:
        ping(url="https://example.com")
        ping(url="https://example.org")
        txn.rollback()

    assert calls == []  # the world is untouched
    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    # Staged effects with no snapshot stay STAGED through rollback — the
    # audit story is "they were staged, txn never committed". COMPENSATED
    # would be misleading: nothing fired, nothing was undone.
    assert statuses == ["STAGED", "STAGED"]


def test_exception_in_agent_block_does_not_fire_staged_effects():
    ping, calls = _fire_counter()
    with pytest.raises(RuntimeError, match="agent gave up"):
        with agent_txn({"http": HTTPAdapter()}):
            ping(url="https://example.com")
            raise RuntimeError("agent gave up")

    assert calls == []


def test_compensator_typo_raises_at_stage_time():
    # D2: missing compensator names fail loudly at stage-time. Late discovery
    # (at commit/unwind) would risk a STUCK txn for a fixable typo.
    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="refnud_charge",  # typo!
    )
    def charge(amount):
        return "ch_1"

    with pytest.raises(CompensatorNotRegistered, match="refnud_charge"):
        with agent_txn({"http": HTTPAdapter()}):
            charge(amount=100)


def test_compensator_registered_in_either_order_works():
    # Tests run in import order — the compensator function might be
    # defined after the original tool. As long as both are registered
    # before stage-time, lookup succeeds.
    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="refund",
    )
    def charge(amount):
        return "ch_1"

    @tool(resource="http", reversible=False, injects_handle=False)
    def refund(amount):
        return None

    with agent_txn({"http": HTTPAdapter()}) as txn:
        charge(amount=100)
        txn.rollback()  # don't actually fire; just confirm stage-time OK
