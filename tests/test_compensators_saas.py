"""SaaS-API compensators tested as left-inverses + the SMS gate.

Covers GitHub (PR, label), Slack (message), Stripe (customer), SendGrid
(contact), Jira (issue) as clean inverses, and Twilio SMS as a gate. See
``test_compensators_payments`` for the tripwire pattern that drives the
real fire → compensate path; each domain registers its own tripwire on its
own resource key so the catalog's per-domain ``resource=`` defaults are
exercised end to end.
"""

from __future__ import annotations

import pytest

from pherix.compensators.saas import (
    register_jira_create_delete_issue,
    register_github_label,
    register_github_pr,
    register_sendgrid_contact,
    register_slack_message,
    register_stripe_customer,
    register_twilio_sms_gate,
)
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


def _tripwire(resource: str):
    @tool(resource=resource, reversible=False, injects_handle=False, name="_tw_undo")
    def _tw_undo():
        pass

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="_tw_undo",
        name="_tw",
    )
    def tripwire():
        raise RuntimeError("boom")

    return tripwire


class FakeSaasClient:
    def __init__(self):
        self.prs: set[tuple[str, str]] = set()
        self.labels: set[tuple[str, str, str]] = set()
        self.messages: set[tuple[str, str]] = set()
        self.customers: set[str] = set()
        self.contacts: set[tuple[str, str]] = set()
        self.issues: set[str] = set()
        self.sms: list[dict] = []

    # GitHub
    def create_pr(self, repo, branch, title, body):
        self.prs.add((repo, branch))

    def close_pr(self, repo, branch):
        self.prs.discard((repo, branch))

    def add_label(self, repo, issue, label):
        self.labels.add((repo, issue, label))

    def remove_label(self, repo, issue, label):
        self.labels.discard((repo, issue, label))

    # Slack
    def post_message(self, channel, ts, text):
        self.messages.add((channel, ts))

    def delete_message(self, channel, ts):
        self.messages.discard((channel, ts))

    # Stripe
    def create_customer(self, customer_id, email):
        self.customers.add(customer_id)

    def delete_customer(self, customer_id):
        self.customers.discard(customer_id)

    # SendGrid
    def add_contact(self, list_id, email):
        self.contacts.add((list_id, email))

    def remove_contact(self, list_id, email):
        self.contacts.discard((list_id, email))

    # Jira
    def create_issue(self, issue_key, project, summary):
        self.issues.add(issue_key)

    def delete_issue(self, issue_key):
        self.issues.discard(issue_key)

    # Twilio
    def send_sms(self, to, body):
        self.sms.append({"to": to, "body": body})


# --- GitHub PR ------------------------------------------------------------


def test_github_pr_left_inverse():
    client = FakeSaasClient()
    create_pr, _ = register_github_pr(client)
    tw = _tripwire("github")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"github": HTTPAdapter()}):
            create_pr(repo="acme/app", branch="feat/x", title="t", body="b")
            tw()
    assert client.prs == set()


def test_github_pr_clean_commit():
    client = FakeSaasClient()
    create_pr, _ = register_github_pr(client)
    with agent_txn({"github": HTTPAdapter()}) as txn:
        create_pr(repo="acme/app", branch="feat/x", title="t", body="b")
    assert txn.txn.state is TxnState.COMMITTED
    assert ("acme/app", "feat/x") in client.prs


# --- GitHub label ---------------------------------------------------------


def test_github_label_left_inverse():
    client = FakeSaasClient()
    add_label, _ = register_github_label(client)
    tw = _tripwire("github")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"github": HTTPAdapter()}):
            add_label(repo="acme/app", issue="42", label="bug")
            tw()
    assert client.labels == set()


# --- Slack message --------------------------------------------------------


def test_slack_message_left_inverse():
    client = FakeSaasClient()
    post, _ = register_slack_message(client)
    tw = _tripwire("slack")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"slack": HTTPAdapter()}):
            post(channel="C1", ts="171.5", text="hi")
            tw()
    assert client.messages == set()


# --- Stripe customer ------------------------------------------------------


def test_stripe_customer_left_inverse():
    client = FakeSaasClient()
    create, _ = register_stripe_customer(client)
    tw = _tripwire("stripe")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"stripe": HTTPAdapter()}):
            create(customer_id="cus_1", email="a@example.com")
            tw()
    assert client.customers == set()


# --- SendGrid contact -----------------------------------------------------


def test_sendgrid_contact_left_inverse():
    client = FakeSaasClient()
    add, _ = register_sendgrid_contact(client)
    tw = _tripwire("sendgrid")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"sendgrid": HTTPAdapter()}):
            add(list_id="l1", email="a@example.com")
            tw()
    assert client.contacts == set()


# --- Jira issue -----------------------------------------------------------


def test_jira_issue_left_inverse():
    client = FakeSaasClient()
    create, _ = register_jira_create_delete_issue(client)
    tw = _tripwire("jira")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"jira": HTTPAdapter()}):
            create(issue_key="PROJ-1", project="PROJ", summary="s")
            tw()
    assert client.issues == set()


# --- Twilio SMS → GATE ----------------------------------------------------


def test_twilio_sms_gates_at_commit():
    client = FakeSaasClient()
    send_sms = register_twilio_sms_gate(client)
    with pytest.raises(GateBlocked) as excinfo:
        with agent_txn({"twilio": HTTPAdapter()}):
            send_sms(to="+15551234567", body="hi")
    assert client.sms == []
    assert len(excinfo.value.needs_approval) == 1


def test_twilio_sms_fires_after_approval():
    client = FakeSaasClient()
    send_sms = register_twilio_sms_gate(client)
    with agent_txn({"twilio": HTTPAdapter()}) as txn:
        r = send_sms(to="+15551234567", body="hi")
        txn.approve_irreversible(r.effect_id)
    assert txn.txn.state is TxnState.COMMITTED
    assert client.sms == [{"to": "+15551234567", "body": "hi"}]


# --- cross-domain partial unwind ------------------------------------------


def test_saas_cross_domain_partial_unwind():
    """A PR and a Slack post across two resources, then a raise → both
    inverses fire, every domain restored. Drives the multi-adapter path."""
    client = FakeSaasClient()
    create_pr, _ = register_github_pr(client)
    post, _ = register_slack_message(client)
    tw = _tripwire("github")
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"github": HTTPAdapter(), "slack": HTTPAdapter()}):
            create_pr(repo="acme/app", branch="feat/x", title="t", body="b")
            post(channel="C1", ts="171.5", text="hi")
            tw()
    assert client.prs == set()
    assert client.messages == set()
