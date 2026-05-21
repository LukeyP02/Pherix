"""MQAdapter — irreversible publish/pub-sub adapter + harness.

All offline: an in-memory fake broker records published messages; no real
broker, no network.
"""

from __future__ import annotations

import pytest

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
)
from pherix.core.adapters.http import IrreversibleAdapterError
from pherix.core.adapters.messagequeue import (
    Broker,
    MQAdapter,
    publish_tool,
    tombstone_compensator,
)
from pherix.core.effects import Effect, EffectStatus
from pherix.core.runtime import GateBlocked, agent_txn


class FakeBroker:
    """In-memory broker — collects every published (topic, message)."""

    def __init__(self, raises: Exception | None = None):
        self.published: list[tuple] = []
        self.raises = raises

    def publish(self, topic, message):
        if self.raises is not None:
            raise self.raises
        self.published.append((topic, message))
        return {"acked": True, "offset": len(self.published) - 1}


def _effect(**overrides):
    base = dict(
        txn_id="txn-1",
        index=0,
        tool="emit",
        args={"topic": "orders", "message": {"id": 1}},
        resource="mq",
        reversible=False,
    )
    base.update(overrides)
    return Effect(**base)


# --- protocol / honesty -------------------------------------------------------


def test_conforms_to_resource_adapter_protocol():
    assert isinstance(MQAdapter(), ResourceAdapter)


def test_does_not_conform_to_transactional_sub_protocol():
    assert not isinstance(MQAdapter(), TransactionalResourceAdapter)


def test_fake_broker_satisfies_broker_protocol():
    assert isinstance(FakeBroker(), Broker)


def test_supports_rollback_is_false():
    assert MQAdapter().supports_rollback() is False


def test_snapshot_raises_irreversible():
    with pytest.raises(IrreversibleAdapterError):
        MQAdapter().snapshot(_effect())


def test_restore_raises_irreversible():
    with pytest.raises(IrreversibleAdapterError):
        MQAdapter().restore(SnapshotHandle(resource="mq", effect_index=0))


def test_read_and_write_version_raise_irreversible():
    with pytest.raises(IrreversibleAdapterError):
        MQAdapter().read_version(("mq", "k"))
    with pytest.raises(IrreversibleAdapterError):
        MQAdapter().write_version(("mq", "k"))


def test_apply_invokes_tool_with_bound_args_no_handle():
    seen: list[dict] = []

    def fake_tool(topic, message):
        seen.append({"topic": topic, "message": message})
        return {"acked": True}

    result = MQAdapter().apply(_effect(), fake_tool)
    assert seen == [{"topic": "orders", "message": {"id": 1}}]
    assert result == {"acked": True}


# --- harness: publish_tool ----------------------------------------------------


def test_publish_tool_outside_txn_passes_through_to_broker():
    broker = FakeBroker()
    emit = publish_tool("emit_order", broker=broker)
    out = emit(topic="orders", message={"id": 1})
    assert out["acked"] is True
    assert broker.published == [("orders", {"id": 1})]


def test_publish_does_not_fire_at_stage_time_fires_once_at_commit():
    broker = FakeBroker()
    emit = publish_tool("emit_order", broker=broker)
    with agent_txn({"mq": MQAdapter()}) as txn:
        r = emit(topic="orders", message={"id": 1})
        assert broker.published == []  # staged, not published yet
        txn.approve_irreversible(r.effect_id)
    assert broker.published == [("orders", {"id": 1})]
    assert txn.txn.effects[0].status is EffectStatus.APPLIED


def test_rollback_before_commit_never_publishes():
    broker = FakeBroker()
    emit = publish_tool("emit_order", broker=broker)
    with agent_txn({"mq": MQAdapter()}) as txn:
        emit(topic="orders", message={"id": 1})
        txn.rollback()
    assert broker.published == []  # never sent
    assert txn.txn.effects[0].status is EffectStatus.STAGED


def test_publish_gates_without_compensator_or_approval():
    broker = FakeBroker()
    emit = publish_tool("emit_order", broker=broker)
    with pytest.raises(GateBlocked):
        with agent_txn({"mq": MQAdapter()}):
            emit(topic="orders", message={"id": 1})
    assert broker.published == []


def test_publish_broker_error_marks_failed():
    broker = FakeBroker(raises=RuntimeError("broker unreachable"))
    emit = publish_tool("emit_order", broker=broker)
    with pytest.raises(RuntimeError, match="broker unreachable"):
        with agent_txn({"mq": MQAdapter()}) as txn:
            r = emit(topic="orders", message={"id": 1})
            txn.approve_irreversible(r.effect_id)
    assert txn.txn.effects[0].status is EffectStatus.FAILED


# --- harness: tombstone compensator (end-to-end) ------------------------------


def _failing_publish(name="boom"):
    # A second irreversible effect that fails during the commit fold, so the
    # runtime walks back and fires the earlier compensator-backed publish's
    # compensator. This is the genuine end-to-end compensation path for
    # irreversible effects (rollback() before commit never fires them at all).
    return publish_tool(
        name, broker=FakeBroker(raises=RuntimeError("broker down"))
    )


def test_publish_with_tombstone_compensator_publishes_inverse_on_partial_failure():
    broker = FakeBroker()
    tombstone_compensator("cancel_order", broker=broker)
    emit = publish_tool("emit_order", broker=broker, compensator="cancel_order")
    boom = _failing_publish()

    with pytest.raises(RuntimeError, match="broker down"):
        with agent_txn({"mq": MQAdapter()}) as txn:
            emit(topic="orders", message={"id": 1})
            r2 = boom(topic="x", message={})
            txn.approve_irreversible(r2.effect_id)

    # Publish fired at commit; tombstone fired on the backward unwind, same topic.
    assert broker.published == [
        ("orders", {"id": 1}),
        ("orders", {"tombstone": {"id": 1}}),
    ]
    assert txn.txn.effects[0].status is EffectStatus.COMPENSATED


def test_tombstone_compensator_custom_mapping():
    broker = FakeBroker()
    # A broker that supports deletion: the tombstone is a delete-marker.
    tombstone_compensator(
        "cancel_order",
        broker=broker,
        tombstone=lambda m: {"op": "delete", "key": m["id"]},
    )
    emit = publish_tool("emit_order", broker=broker, compensator="cancel_order")
    boom = _failing_publish()
    with pytest.raises(RuntimeError, match="broker down"):
        with agent_txn({"mq": MQAdapter()}) as txn:
            emit(topic="orders", message={"id": 7})
            r2 = boom(topic="x", message={})
            txn.approve_irreversible(r2.effect_id)
    assert broker.published[1] == ("orders", {"op": "delete", "key": 7})


def test_compensator_receives_original_topic_and_message():
    # Confirms the runtime passes the ORIGINAL publish's args to the
    # compensator (args=effect.args), so the tombstone lands on the right topic.
    seen: list[tuple] = []

    sink = FakeBroker()

    def recording_tombstone(message):
        seen.append(message)
        return {"tombstone": message}

    tombstone_compensator("cancel", broker=sink, tombstone=recording_tombstone)
    emit = publish_tool("emit", broker=sink, compensator="cancel")
    boom = _failing_publish()
    with pytest.raises(RuntimeError, match="broker down"):
        with agent_txn({"mq": MQAdapter()}) as txn:
            emit(topic="billing", message={"amount": 50})
            r2 = boom(topic="x", message={})
            txn.approve_irreversible(r2.effect_id)
    # tombstone() saw the original message; published onto the original topic.
    assert seen == [{"amount": 50}]
    assert sink.published[1][0] == "billing"
