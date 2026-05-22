"""Provisioning compensators tested as left-inverses.

``scale_up → scale_down`` is the interesting one: the inverse is only
correct because the action carries the *before-value* (``from_replicas``) in
its args — the engine fires the compensator with the action's args, so the
prior capacity must live there. The test pins that the resource is scaled
back to exactly the before-value, not merely "scaled down".

See ``test_compensators_payments`` for the tripwire pattern that drives the
real fire → compensate path.
"""

from __future__ import annotations

import pytest

from pherix.compensators.provisioning import (
    register_create_delete_resource,
    register_scale_up_down,
)
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


def _tripwire():
    @tool(resource="provisioning", reversible=False, injects_handle=False)
    def _tripwire_undo():
        pass

    @tool(
        resource="provisioning",
        reversible=False,
        injects_handle=False,
        compensator="_tripwire_undo",
    )
    def tripwire():
        raise RuntimeError("boom")

    return tripwire


class FakeInfraClient:
    def __init__(self):
        self.resources: dict[str, dict] = {}
        # Capacity per target; seed an existing service at 2 replicas so the
        # scale-back-to-before-value assertion is non-trivial.
        self.replicas: dict[str, int] = {"svc": 2}

    def create_resource(self, resource_id, kind, spec):
        self.resources[resource_id] = {"kind": kind, "spec": spec}
        return {"id": resource_id}

    def delete_resource(self, resource_id):
        self.resources.pop(resource_id, None)
        return {"id": resource_id, "deleted": True}

    def scale(self, target, replicas):
        self.replicas[target] = replicas
        return {"target": target, "replicas": replicas}


# --- create_resource → delete_resource ------------------------------------


def test_create_delete_left_inverse():
    client = FakeInfraClient()
    create, _ = register_create_delete_resource(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"provisioning": HTTPAdapter()}):
            create(resource_id="r1", kind="bucket", spec={"region": "us"})
            tripwire()

    assert client.resources == {}  # delete ∘ create ≈ identity


def test_create_clean_commit():
    client = FakeInfraClient()
    create, _ = register_create_delete_resource(client)

    with agent_txn({"provisioning": HTTPAdapter()}) as txn:
        create(resource_id="r1", kind="bucket", spec={"region": "us"})

    assert txn.txn.state is TxnState.COMMITTED
    assert "r1" in client.resources


# --- scale_up → scale_down (before-value carried in args) -----------------


def test_scale_up_down_restores_before_value():
    client = FakeInfraClient()
    assert client.replicas["svc"] == 2  # baseline
    scale_up, _ = register_scale_up_down(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"provisioning": HTTPAdapter()}):
            scale_up(target="svc", from_replicas=2, to_replicas=10)
            tripwire()

    # scale_down restores the exact before-value from the args, not 0/1.
    assert client.replicas["svc"] == 2


def test_scale_up_clean_commit():
    client = FakeInfraClient()
    scale_up, _ = register_scale_up_down(client)

    with agent_txn({"provisioning": HTTPAdapter()}) as txn:
        scale_up(target="svc", from_replicas=2, to_replicas=10)

    assert txn.txn.state is TxnState.COMMITTED
    assert client.replicas["svc"] == 10  # stays scaled up — no rollback


def test_provisioning_partial_failure_unwinds_both():
    client = FakeInfraClient()
    create, _ = register_create_delete_resource(client)
    scale_up, _ = register_scale_up_down(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"provisioning": HTTPAdapter()}):
            create(resource_id="r1", kind="bucket", spec={})
            scale_up(target="svc", from_replicas=2, to_replicas=10)
            tripwire()

    assert client.resources == {}
    assert client.replicas["svc"] == 2
