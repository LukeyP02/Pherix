"""Identity & comms compensators tested as left-inverses + the gate path.

See ``test_compensators_payments`` for the engine fact these tests turn on:
irreversible effects fire only during ``commit()``'s forward fold, so the
golden / partial tests use a ``tripwire`` that fires last and raises to
drive the real fire → compensate path. ``send_email`` is the gate case: no
compensator → ``commit()`` blocks until ``approve_irreversible()``.
"""

from __future__ import annotations

import pytest

from pherix.compensators.identity import (
    register_grant_revoke_role,
    register_invite_revoke,
    register_send_email_gate,
)
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


def _tripwire():
    @tool(resource="identity", reversible=False, injects_handle=False)
    def _tripwire_undo():
        pass

    @tool(
        resource="identity",
        reversible=False,
        injects_handle=False,
        compensator="_tripwire_undo",
    )
    def tripwire():
        raise RuntimeError("boom")

    return tripwire


class FakeIdentityClient:
    def __init__(self):
        self.invites: set[str] = set()
        self.roles: set[tuple[str, str]] = set()
        self.sent_emails: list[dict] = []

    def invite(self, invite_id, email, org):
        self.invites.add(invite_id)
        return {"invite_id": invite_id}

    def revoke_invite(self, invite_id):
        self.invites.discard(invite_id)
        return {"invite_id": invite_id, "revoked": True}

    def grant_role(self, principal, role):
        self.roles.add((principal, role))

    def revoke_role(self, principal, role):
        self.roles.discard((principal, role))

    def send_email(self, to, subject, body):
        self.sent_emails.append({"to": to, "subject": subject, "body": body})
        return {"queued": True}


# --- invite → revoke_invite -----------------------------------------------


def test_invite_revoke_left_inverse():
    client = FakeIdentityClient()
    invite, _ = register_invite_revoke(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"identity": HTTPAdapter()}):
            invite(invite_id="inv_1", email="a@example.com", org="acme")
            tripwire()

    assert client.invites == set()  # revoke_invite ∘ invite ≈ identity


def test_invite_clean_commit_no_revoke():
    client = FakeIdentityClient()
    invite, _ = register_invite_revoke(client)

    with agent_txn({"identity": HTTPAdapter()}) as txn:
        invite(invite_id="inv_1", email="a@example.com", org="acme")

    assert txn.txn.state is TxnState.COMMITTED
    assert client.invites == {"inv_1"}  # stays — no rollback


# --- grant_role → revoke_role ---------------------------------------------


def test_grant_revoke_role_left_inverse():
    client = FakeIdentityClient()
    grant, _ = register_grant_revoke_role(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"identity": HTTPAdapter()}):
            grant(principal="alice", role="admin")
            tripwire()

    assert client.roles == set()


def test_identity_partial_failure_unwinds_both():
    """invite + grant_role both fire, tripwire raises → both inverses fire."""
    client = FakeIdentityClient()
    invite, _ = register_invite_revoke(client)
    grant, _ = register_grant_revoke_role(client)
    tripwire = _tripwire()

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"identity": HTTPAdapter()}):
            invite(invite_id="inv_1", email="a@example.com", org="acme")
            grant(principal="alice", role="admin")
            tripwire()

    assert client.invites == set()
    assert client.roles == set()


# --- send_email → GATE (no compensator) -----------------------------------


def test_send_email_gates_at_commit():
    client = FakeIdentityClient()
    send_email = register_send_email_gate(client)

    with pytest.raises(GateBlocked) as excinfo:
        with agent_txn({"identity": HTTPAdapter()}):
            send_email(to="a@example.com", subject="hi", body="hello")
        # auto-commit on clean exit hits the gate

    assert client.sent_emails == []  # blocked — never fired
    assert len(excinfo.value.needs_approval) == 1


def test_send_email_fires_after_approval():
    client = FakeIdentityClient()
    send_email = register_send_email_gate(client)

    with agent_txn({"identity": HTTPAdapter()}) as txn:
        result = send_email(to="a@example.com", subject="hi", body="hello")
        txn.approve_irreversible(result.effect_id)

    assert txn.txn.state is TxnState.COMMITTED
    assert client.sent_emails == [
        {"to": "a@example.com", "subject": "hi", "body": "hello"}
    ]
