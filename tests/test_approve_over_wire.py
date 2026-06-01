"""Approve over the wire — the human gate cleared from OUTSIDE the agent's process.

Today's ``approve_irreversible`` is in-process only: the party clearing the gate
must hold the live ``TxnContext``. This suite pins the over-the-wire path: a
gated commit yields a :class:`PendingApproval` handle carrying a stable token; a
*separate* journal connection (the proxy/MCP gateway, standing in for a reviewer
in another process) records an APPROVED entry against the SAME on-disk journal;
and a resumed ``commit(pending_approval=True)`` reads that journalled approval
and fires the effect.

Three load-bearing claims, each red against origin/main:

1. The gated effect fires AFTER an over-the-wire ``approve(token)`` and NOT
   before — driven through a real ``agent_txn`` with a real HTTP-style effect.
2. The approval is journalled with the approver identity (the #40 actor model).
3. The read-only inspector stays read-only — no write path is introduced;
   approving through it raises rather than mutating the journal under audit.
"""

from __future__ import annotations

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus, PendingApproval
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState
from pherix.frontends.proxy import InProcessMCPClient, PherixGateway

# Trust pillar: oversight — the gate, now cleared across a process boundary.
pytestmark = pytest.mark.oversight


def _make_send_email():
    """An irreversible tool with no compensator — needs approval to commit."""
    calls: list[dict] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, body):
        calls.append({"to": to, "body": body})
        return {"sent": to}

    return send_email, calls


def test_pending_approval_is_public_surface():
    """``PendingApproval`` is re-exported at the package + library top level."""
    import pherix
    from pherix.frontends.library import PendingApproval as LibPA

    assert pherix.PendingApproval is PendingApproval
    assert LibPA is PendingApproval


# --- claim 1: the effect fires AFTER an over-the-wire approve, not before ----


def test_gated_effect_fires_after_over_the_wire_approve_not_before(tmp_path):
    """The whole point, end to end across two journal connections.

    Agent process: stage an irreversible effect, ``commit(pending_approval=
    True)`` gate-blocks and yields a token without firing. Approver process
    (a second journal connection on the same DB file): record the approval.
    Agent process: resume the commit — now the effect fires.
    """
    journal_path = str(tmp_path / "journal.db")
    send_email, calls = _make_send_email()

    # --- agent process: open a txn, stage the effect, request approval -------
    agent_audit = AuditJournal(journal_path)
    with agent_txn({"http": HTTPAdapter()}, audit=agent_audit) as txn:
        result = send_email(to="alice@example.com", body="hi")
        pending = txn.commit(pending_approval=True)

        # Gate blocked: a handle came back, the effect did NOT fire, and the
        # txn is still open (held for resume, not rolled back).
        assert len(pending) == 1
        assert isinstance(pending[0], PendingApproval)
        assert pending[0].effect_id == result.effect_id
        token = pending[0].token
        assert calls == []  # NOT before approval
        assert txn.txn.state is TxnState.OPEN

        # --- approver process: a DIFFERENT connection on the same journal ---
        # This is the over-the-wire write — no access to the live TxnContext.
        approver_audit = AuditJournal(journal_path)
        approver_audit.record_approval(token, approver="reviewer-bob")
        approver_audit.close()

        # Still not fired: recording the approval touches only the journal.
        assert calls == []

        # --- agent process: resume the commit — the journalled approval lets
        # the gate pass and the effect fires.
        resumed = txn.commit(pending_approval=True)

    assert resumed == []  # clean commit, no further pending handles
    assert calls == [{"to": "alice@example.com", "body": "hi"}]  # fired AFTER
    assert txn.txn.effects[0].status is EffectStatus.APPLIED
    assert txn.txn.state is TxnState.COMMITTED
    agent_audit.close()


def test_resume_without_approval_stays_pending_and_does_not_fire(tmp_path):
    """A resumed commit with no approval recorded re-gates — never fires."""
    journal_path = str(tmp_path / "journal.db")
    send_email, calls = _make_send_email()
    audit = AuditJournal(journal_path)
    with agent_txn({"http": HTTPAdapter()}, audit=audit) as txn:
        send_email(to="a@example.com", body="x")
        first = txn.commit(pending_approval=True)
        assert len(first) == 1
        # No approval recorded between the two commits.
        second = txn.commit(pending_approval=True)
        assert len(second) == 1
        assert second[0].token == first[0].token  # stable token, same effect
        assert calls == []
        # Clear it so the with-block can finish cleanly.
        audit.record_approval(first[0].token, approver="late")
        txn.commit(pending_approval=True)
    assert calls == [{"to": "a@example.com", "body": "x"}]
    audit.close()


def test_approve_through_proxy_gateway_fires_resumed_commit(tmp_path):
    """The approval travels through the proxy/MCP gateway's ``approve`` op.

    The gateway is the front-end the spec names. It holds a journal on the
    shared path; ``client.approve(token)`` records the APPROVED entry; the
    agent's resumed commit fires.
    """
    journal_path = str(tmp_path / "journal.db")
    send_email, calls = _make_send_email()

    agent_audit = AuditJournal(journal_path)
    with agent_txn({"http": HTTPAdapter()}, audit=agent_audit) as txn:
        send_email(to="ops@example.com", body="deploy")
        pending = txn.commit(pending_approval=True)
        token = pending[0].token
        assert calls == []

        # Approve through the gateway front-end (its own journal connection).
        gateway = PherixGateway(
            adapters={"http": HTTPAdapter()},
            default_policy=Policy.allow_all(),
            audit=AuditJournal(journal_path),
        )
        client = InProcessMCPClient(gateway)
        client.initialize("reviewer-svc")
        resp = client.approve(token)
        row = client.result_of(resp)
        assert row["approved"] is True
        assert row["status"] == "APPROVED"
        assert calls == []  # gateway write alone fires nothing

        txn.commit(pending_approval=True)

    assert calls == [{"to": "ops@example.com", "body": "deploy"}]
    agent_audit.close()


# --- claim 2: the approval is journalled WITH the approver identity ----------


def test_approval_journalled_with_approver_identity(tmp_path):
    journal_path = str(tmp_path / "journal.db")
    send_email, _ = _make_send_email()

    agent_audit = AuditJournal(journal_path)
    with agent_txn({"http": HTTPAdapter()}, audit=agent_audit) as txn:
        send_email(to="a@example.com", body="x")
        pending = txn.commit(pending_approval=True)
        token = pending[0].token
        txn_id = txn.txn_id

        # PENDING is journalled at gate time, with no approver yet.
        pre = agent_audit.get_approvals(txn_id)
        assert len(pre) == 1
        assert pre[0]["status"] == "PENDING"
        assert pre[0]["approver"] is None
        assert pre[0]["token"] == token

        # The over-the-wire approval stamps the approver.
        approver_audit = AuditJournal(journal_path)
        approver_audit.record_approval(token, approver="role:release-manager")
        approver_audit.close()

        txn.commit(pending_approval=True)

    rows = agent_audit.get_approvals(txn_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "APPROVED"
    assert rows[0]["approver"] == "role:release-manager"  # identity recorded
    assert rows[0]["approved_at"] is not None
    agent_audit.close()


def test_gateway_approve_attributes_session_identity_when_no_approver(tmp_path):
    """Through the gateway, the approver defaults to the session identity."""
    journal_path = str(tmp_path / "journal.db")
    send_email, _ = _make_send_email()

    agent_audit = AuditJournal(journal_path)
    with agent_txn({"http": HTTPAdapter()}, audit=agent_audit) as txn:
        send_email(to="a@example.com", body="x")
        token = txn.commit(pending_approval=True)[0].token
        txn_id = txn.txn_id

        gateway = PherixGateway(
            adapters={"http": HTTPAdapter()},
            default_policy=Policy.allow_all(),
            audit=AuditJournal(journal_path),
        )
        client = InProcessMCPClient(gateway)
        client.initialize("alice@corp")  # session identity
        client.approve(token)  # no explicit approver
        txn.commit(pending_approval=True)

    rows = agent_audit.get_approvals(txn_id)
    assert rows[0]["approver"] == "alice@corp"  # attributed to the session
    agent_audit.close()


def test_unknown_token_raises_and_journals_nothing(tmp_path):
    journal_path = str(tmp_path / "journal.db")
    audit = AuditJournal(journal_path)
    with pytest.raises(KeyError, match="no pending approval"):
        audit.record_approval("apr-does-not-exist", approver="x")
    audit.close()


def test_gateway_approve_unknown_token_is_jsonrpc_error(tmp_path):
    from pherix.frontends.proxy.server import UNKNOWN_APPROVAL_TOKEN

    gateway = PherixGateway(
        adapters={"http": HTTPAdapter()},
        default_policy=Policy.allow_all(),
        audit=AuditJournal(str(tmp_path / "journal.db")),
    )
    client = InProcessMCPClient(gateway)
    client.initialize("reviewer")
    resp = client.approve("apr-nope")
    assert client.error_of(resp) is not None
    assert resp["error"]["code"] == UNKNOWN_APPROVAL_TOKEN


# --- TOCTOU: policy re-evaluated at the resumed commit -----------------------


class _MutablePolicy(Policy):
    def revoke(self, tool_name: str) -> None:
        self.deny.add(tool_name)


def test_policy_revoked_between_approval_and_resume_still_blocks(tmp_path):
    """The approval landed, but the policy was revoked before the resume.

    The twice-evaluated bracket must re-rule at the resumed commit: an
    out-of-process approval does NOT bypass commit-time policy. This is the
    TOCTOU guarantee surviving the wire round-trip.
    """
    journal_path = str(tmp_path / "journal.db")
    calls: list[dict] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, body):
        calls.append({"to": to})
        return None

    policy = _MutablePolicy()
    audit = AuditJournal(journal_path)
    with pytest.raises(PolicyViolation, match="send_email"):
        with agent_txn(
            {"http": HTTPAdapter()}, policy=policy, audit=audit
        ) as txn:
            send_email(to="a@example.com", body="x")
            token = txn.commit(pending_approval=True)[0].token
            # Approve over the wire...
            AuditJournal(journal_path).record_approval(token, approver="bob")
            # ...but the policy is revoked before the resume.
            policy.revoke("send_email")
            txn.commit(pending_approval=True)  # re-eval blocks
    assert calls == []  # never fired despite the recorded approval
    audit.close()


# --- claim 3: the inspector stays read-only ---------------------------------


def test_inspector_reader_has_no_approval_write_path(tmp_path):
    """The inspector reads approvals but exposes NO method that records one.

    The read-only inspector opens the DB ``mode=ro``; it must never gain a
    write path. We assert two things: (a) the reader exposes no
    ``record_approval`` / ``approve`` surface, and (b) a write attempted
    against its read-only connection genuinely fails at the SQLite layer.
    """
    from pherix.inspector.reader import JournalReader

    # Seed a real approval via the engine so there is a row to read.
    journal_path = str(tmp_path / "journal.db")
    send_email, _ = _make_send_email()
    agent_audit = AuditJournal(journal_path)
    with agent_txn({"http": HTTPAdapter()}, audit=agent_audit) as txn:
        send_email(to="a@example.com", body="x")
        token = txn.commit(pending_approval=True)[0].token
        AuditJournal(journal_path).record_approval(token, approver="bob")
        txn.commit(pending_approval=True)
    agent_audit.close()

    reader = JournalReader(journal_path)
    # (a) No write surface introduced on the reader.
    assert not hasattr(reader, "record_approval")
    assert not hasattr(reader, "approve")
    assert not hasattr(reader, "record_pending_approval")

    # (b) The connection is genuinely read-only — a direct write raises.
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        reader._conn.execute(
            "INSERT INTO approvals "
            "(txn_id, effect_id, token, status, requested_at) "
            "VALUES ('t', 'e', 'apr-x', 'APPROVED', '2026-01-01')"
        )
    reader.close()
