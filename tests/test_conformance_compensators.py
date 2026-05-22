"""Cross-compensator conformance battery — every catalog pair as a left-inverse.

A compensator is a *semantic left-inverse* of an irreversible tool:
``compensator ∘ tool ≈ identity`` on the external world. The catalog in
``pherix/compensators/`` ships vetted pairs (charge→refund, payout→reverse,
invite→revoke, grant→revoke, create→delete, scale_up→scale_down, PR open→close,
label add→remove, message post→delete, customer create→delete, contact
add→remove, issue create→delete). This suite proves *every* registered pair
obeys the left-inverse law, INCLUDING the partial-failure unwind path — as one
parametrized matrix, so a thirteenth pair added to the catalog is one registry
entry here, not a new copy-pasted test.

Why this over the per-domain ``test_compensators_*.py`` files: those prove each
domain in isolation, with bespoke fake clients. This suite drives every pair
through a *single uniform world model* and the same three laws, so a regression
in any pair — or a new pair wired to reverse off the wrong key — fails here
against the identical assertion.

The engine fact every test is built around (same as the per-domain suites):
irreversible effects do NOT fire at stage-time. They stage and fire only inside
``commit()``'s forward fold. So to exercise a *genuine* fire → compensate, the
action must actually fire and then be unwound — which the engine does when a
LATER staged irreversible raises during the commit forward fold. Every
golden/partial test registers a ``tripwire`` irreversible that fires last and
raises, driving the real path. A body that merely ``raise``s before commit
would leave every action STAGED-and-never-fired, and a "world == baseline"
assertion would pass *vacuously* — which the ``*_calls`` counters guard against.

The world is a single dict of sets/maps; the fake client is duck-typed and
records every action/inverse into it, so "the world returned to its pre-txn
meaning" is a concrete ``==`` on the recorded state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from pherix.compensators.identity import (
    register_grant_revoke_role,
    register_invite_revoke,
    register_send_email_gate,
)
from pherix.compensators.payments import (
    register_charge_refund,
    register_payout_reverse,
)
from pherix.compensators.saas import (
    register_github_label,
    register_github_pr,
    register_jira_create_delete_issue,
    register_sendgrid_contact,
    register_slack_message,
    register_stripe_customer,
    register_twilio_sms_gate,
)
from pherix.compensators.provisioning import (
    register_create_delete_resource,
    register_scale_up_down,
)
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


# ===========================================================================
# A uniform recording fake client.
#
# Every catalog client is duck-typed (the kernel never imports stripe / boto3 /
# PyGithub). We model one universal external world as a dict of named sets/maps
# and record every action/inverse method into it. Each method also bumps a
# per-method call counter so a test can assert the action *genuinely fired*
# (not a vacuous never-staged pass) and the inverse *genuinely ran*.
#
# The recording is content-addressed by the action's KEY ARGS — exactly the
# idempotency-key pattern the catalog reverses off (the compensator only ever
# sees the action's args, never its return value). So an inverse that reverses
# off the right key removes precisely what the action added; one that reverses
# off the wrong key leaves the world dirty, and the left-inverse law fails here.
# ===========================================================================


class RecordingClient:
    """Duck-typed fake recording the whole catalog's surface into ``self.world``.

    Each method mutates a named slice of ``world`` keyed by the *same* args the
    real provider keys on, and bumps ``calls[method]``. Maps (charge/payout)
    track a value; sets (invites/PRs/…) track presence.
    """

    def __init__(self) -> None:
        # maps: key -> value
        self.charges: dict[str, int] = {}
        self.payouts: dict[str, int] = {}
        self.scales: dict[str, int] = {}
        # sets: presence
        self.invites: set[str] = set()
        self.roles: set[tuple[str, str]] = set()
        self.resources: set[str] = set()
        self.prs: set[tuple[str, str]] = set()
        self.labels: set[tuple[str, str, str]] = set()
        self.messages: set[tuple[str, str]] = set()
        self.customers: set[str] = set()
        self.contacts: set[tuple[str, str]] = set()
        self.issues: set[str] = set()
        # gate-only sinks (no inverse)
        self.emails: list[tuple] = []
        self.sms: list[tuple] = []
        self.calls: dict[str, int] = {}

    def _tick(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def snapshot(self) -> dict:
        """A comparable, deep-ish copy of every world slice."""
        return {
            "charges": dict(self.charges),
            "payouts": dict(self.payouts),
            "scales": dict(self.scales),
            "invites": set(self.invites),
            "roles": set(self.roles),
            "resources": set(self.resources),
            "prs": set(self.prs),
            "labels": set(self.labels),
            "messages": set(self.messages),
            "customers": set(self.customers),
            "contacts": set(self.contacts),
            "issues": set(self.issues),
        }

    # --- payments ---
    def charge(self, idempotency_key, amount_cents, currency):
        self._tick("charge")
        self.charges[idempotency_key] = amount_cents

    def refund(self, idempotency_key):
        self._tick("refund")
        self.charges.pop(idempotency_key, None)

    def payout(self, payout_id, amount_cents, destination):
        self._tick("payout")
        self.payouts[payout_id] = amount_cents

    def reverse_payout(self, payout_id):
        self._tick("reverse_payout")
        self.payouts.pop(payout_id, None)

    # --- provisioning ---
    def create_resource(self, resource_id, kind, spec):
        self._tick("create_resource")
        self.resources.add(resource_id)

    def delete_resource(self, resource_id):
        self._tick("delete_resource")
        self.resources.discard(resource_id)

    def scale(self, target, replicas):
        # scale_up sets to_replicas; scale_down restores from_replicas.
        self._tick("scale")
        self.scales[target] = replicas

    # --- identity ---
    def invite(self, invite_id, email, org):
        self._tick("invite")
        self.invites.add(invite_id)

    def revoke_invite(self, invite_id):
        self._tick("revoke_invite")
        self.invites.discard(invite_id)

    def grant_role(self, principal, role):
        self._tick("grant_role")
        self.roles.add((principal, role))

    def revoke_role(self, principal, role):
        self._tick("revoke_role")
        self.roles.discard((principal, role))

    def send_email(self, to, subject, body):
        self._tick("send_email")
        self.emails.append((to, subject, body))

    # --- saas ---
    def create_pr(self, repo, branch, title, body):
        self._tick("create_pr")
        self.prs.add((repo, branch))

    def close_pr(self, repo, branch):
        self._tick("close_pr")
        self.prs.discard((repo, branch))

    def add_label(self, repo, issue, label):
        self._tick("add_label")
        self.labels.add((repo, issue, label))

    def remove_label(self, repo, issue, label):
        self._tick("remove_label")
        self.labels.discard((repo, issue, label))

    def post_message(self, channel, ts, text):
        self._tick("post_message")
        self.messages.add((channel, ts))

    def delete_message(self, channel, ts):
        self._tick("delete_message")
        self.messages.discard((channel, ts))

    def create_customer(self, customer_id, email):
        self._tick("create_customer")
        self.customers.add(customer_id)

    def delete_customer(self, customer_id):
        self._tick("delete_customer")
        self.customers.discard(customer_id)

    def add_contact(self, list_id, email):
        self._tick("add_contact")
        self.contacts.add((list_id, email))

    def remove_contact(self, list_id, email):
        self._tick("remove_contact")
        self.contacts.discard((list_id, email))

    def create_issue(self, issue_key, project, summary):
        self._tick("create_issue")
        self.issues.add(issue_key)

    def delete_issue(self, issue_key):
        self._tick("delete_issue")
        self.issues.discard(issue_key)

    def send_sms(self, to, body):
        self._tick("send_sms")
        self.sms.append((to, body))


# ===========================================================================
# The pair matrix. Each entry knows: how to register the pair, the resource key
# its tools use, the action name + the kwargs to call it with, the inverse name,
# and (for the scale case) any pre-seeded world state the inverse restores to.
# ===========================================================================


@dataclass
class PairCase:
    name: str
    register: Callable[[Any], tuple]  # (client) -> (action_fn, comp_fn)
    resource: str
    action_kwargs: dict
    action_method: str  # client method name the action calls — for the counter
    comp_method: str  # client method name the compensator calls
    # Optional second call's kwargs, to assert two distinct actions both unwind.
    second_kwargs: dict | None = None
    # World seeding run BEFORE the txn (e.g. scale needs a starting replica
    # count so scale_down has somewhere to return to). ``seed(client)``.
    seed: Callable[[Any], None] | None = None


PAIR_CASES = [
    PairCase(
        name="charge_refund",
        register=register_charge_refund,
        resource="payments",
        action_kwargs={"idempotency_key": "ik_1", "amount_cents": 5000},
        action_method="charge",
        comp_method="refund",
        second_kwargs={"idempotency_key": "ik_2", "amount_cents": 250},
    ),
    PairCase(
        name="payout_reverse",
        register=register_payout_reverse,
        resource="payments",
        action_kwargs={"payout_id": "po_1", "amount_cents": 9000, "destination": "acct_1"},
        action_method="payout",
        comp_method="reverse_payout",
        second_kwargs={"payout_id": "po_2", "amount_cents": 10, "destination": "acct_2"},
    ),
    PairCase(
        name="invite_revoke",
        register=register_invite_revoke,
        resource="identity",
        action_kwargs={"invite_id": "inv_1", "email": "a@b.c", "org": "acme"},
        action_method="invite",
        comp_method="revoke_invite",
        second_kwargs={"invite_id": "inv_2", "email": "d@e.f", "org": "acme"},
    ),
    PairCase(
        name="grant_revoke_role",
        register=register_grant_revoke_role,
        resource="identity",
        action_kwargs={"principal": "user:alice", "role": "admin"},
        action_method="grant_role",
        comp_method="revoke_role",
        second_kwargs={"principal": "user:bob", "role": "editor"},
    ),
    PairCase(
        name="create_delete_resource",
        register=register_create_delete_resource,
        resource="provisioning",
        action_kwargs={"resource_id": "r_1", "kind": "vm", "spec": {"cpu": 2}},
        action_method="create_resource",
        comp_method="delete_resource",
        second_kwargs={"resource_id": "r_2", "kind": "bucket", "spec": {}},
    ),
    PairCase(
        name="scale_up_down",
        register=register_scale_up_down,
        resource="provisioning",
        action_kwargs={"target": "web", "from_replicas": 3, "to_replicas": 10},
        action_method="scale",
        comp_method="scale",
        seed=lambda c: c.scales.update({"web": 3}),
    ),
    PairCase(
        name="github_pr",
        register=register_github_pr,
        resource="github",
        action_kwargs={"repo": "o/r", "branch": "feat", "title": "t", "body": "b"},
        action_method="create_pr",
        comp_method="close_pr",
        second_kwargs={"repo": "o/r", "branch": "fix", "title": "t2", "body": "b2"},
    ),
    PairCase(
        name="github_label",
        register=register_github_label,
        resource="github",
        action_kwargs={"repo": "o/r", "issue": "7", "label": "bug"},
        action_method="add_label",
        comp_method="remove_label",
        second_kwargs={"repo": "o/r", "issue": "7", "label": "p1"},
    ),
    PairCase(
        name="slack_message",
        register=register_slack_message,
        resource="slack",
        action_kwargs={"channel": "C1", "ts": "169.1", "text": "hi"},
        action_method="post_message",
        comp_method="delete_message",
        second_kwargs={"channel": "C1", "ts": "169.2", "text": "yo"},
    ),
    PairCase(
        name="stripe_customer",
        register=register_stripe_customer,
        resource="stripe",
        action_kwargs={"customer_id": "cus_1", "email": "a@b.c"},
        action_method="create_customer",
        comp_method="delete_customer",
        second_kwargs={"customer_id": "cus_2", "email": "d@e.f"},
    ),
    PairCase(
        name="sendgrid_contact",
        register=register_sendgrid_contact,
        resource="sendgrid",
        action_kwargs={"list_id": "L1", "email": "a@b.c"},
        action_method="add_contact",
        comp_method="remove_contact",
        second_kwargs={"list_id": "L1", "email": "d@e.f"},
    ),
    PairCase(
        name="jira_issue",
        register=register_jira_create_delete_issue,
        resource="jira",
        action_kwargs={"issue_key": "PROJ-1", "project": "PROJ", "summary": "s"},
        action_method="create_issue",
        comp_method="delete_issue",
        second_kwargs={"issue_key": "PROJ-2", "project": "PROJ", "summary": "s2"},
    ),
]

# Gate-only entries: no honest inverse, so the action gates at commit.
GATE_CASES = [
    pytest.param(register_send_email_gate, "identity",
                 {"to": "a@b.c", "subject": "hi", "body": "x"}, "send_email",
                 id="send_email_gate"),
    pytest.param(register_twilio_sms_gate, "twilio",
                 {"to": "+1", "body": "x"}, "send_sms",
                 id="send_sms_gate"),
]


def _tripwire(resource: str):
    """A compensator-backed irreversible that fires last and raises, forcing
    the commit-time forward fold to unwind every prior fired irreversible.
    Registered on the SAME resource as the pair under test, so its compensator
    routes through the same HTTPAdapter.
    """

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


# ===========================================================================
# Law 1 — left-inverse / golden: action fires, then unwind returns the world
# to its pre-txn meaning. The action genuinely fires (counter == 1) and the
# inverse genuinely runs (counter == 1).
# ===========================================================================


@pytest.mark.parametrize("case", PAIR_CASES, ids=[c.name for c in PAIR_CASES])
def test_left_inverse_single_action(case: PairCase):
    client = RecordingClient()
    if case.seed:
        case.seed(client)
    baseline = client.snapshot()

    action, _comp = case.register(client)
    tripwire = _tripwire(case.resource)

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({case.resource: HTTPAdapter()}):
            action(**case.action_kwargs)
            tripwire()  # fires after the action, raises → forces unwind

    # compensator ∘ action ≈ identity on the external world.
    assert client.snapshot() == baseline, (
        f"{case.name}: world did not return to baseline after unwind. "
        f"baseline={baseline} after={client.snapshot()}"
    )
    # Non-vacuity: the action genuinely fired and its inverse genuinely ran.
    assert client.calls.get(case.action_method, 0) >= 1
    assert client.calls.get(case.comp_method, 0) >= 1


# ===========================================================================
# Law 2 — partial-failure unwind: TWO distinct actions fire, a later effect
# raises mid-commit, and BOTH inverses fire so the world fully restores.
# ===========================================================================


@pytest.mark.parametrize(
    "case",
    [c for c in PAIR_CASES if c.second_kwargs is not None],
    ids=[c.name for c in PAIR_CASES if c.second_kwargs is not None],
)
def test_partial_failure_unwinds_all_fired_actions(case: PairCase):
    client = RecordingClient()
    if case.seed:
        case.seed(client)
    baseline = client.snapshot()

    action, _comp = case.register(client)
    tripwire = _tripwire(case.resource)

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({case.resource: HTTPAdapter()}):
            action(**case.action_kwargs)
            action(**case.second_kwargs)
            tripwire()

    assert client.snapshot() == baseline, (
        f"{case.name}: partial-failure unwind left the world dirty. "
        f"baseline={baseline} after={client.snapshot()}"
    )
    # Both actions fired and both were inverted.
    assert client.calls.get(case.action_method, 0) >= 2
    assert client.calls.get(case.comp_method, 0) >= 2


# ===========================================================================
# Law 3 — clean commit: the action fires once, the compensator NEVER fires.
# The compensator is the rollback path only; a committed action stays committed.
# ===========================================================================


@pytest.mark.parametrize("case", PAIR_CASES, ids=[c.name for c in PAIR_CASES])
def test_clean_commit_does_not_invert(case: PairCase):
    client = RecordingClient()
    if case.seed:
        case.seed(client)

    action, _comp = case.register(client)

    with agent_txn({case.resource: HTTPAdapter()}) as ctx:
        action(**case.action_kwargs)

    assert ctx.txn.state is TxnState.COMMITTED
    assert client.calls.get(case.action_method, 0) == 1
    # The inverse must not run on a clean commit. (For scale_up/down the action
    # and inverse share the `scale` method, so guard only the distinct ones.)
    if case.comp_method != case.action_method:
        assert client.calls.get(case.comp_method, 0) == 0


# ===========================================================================
# Law 4 — adversarial: a compensator that itself raises on the unwind path
# lands the txn STUCK and leaves the action recorded for manual recovery.
# Asserted once (the engine behaviour is pair-independent) on charge→refund.
# ===========================================================================


def test_failing_compensator_lands_txn_stuck():
    class BrokenClient(RecordingClient):
        def refund(self, idempotency_key):
            self._tick("refund")
            raise RuntimeError("refund endpoint 500")

    client = BrokenClient()
    charge, _refund = register_charge_refund(client)
    tripwire = _tripwire("payments")

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"payments": HTTPAdapter()}) as ctx:
            charge(idempotency_key="ik_1", amount_cents=5000)
            tripwire()

    assert ctx.txn.state is TxnState.STUCK
    # The charge stays recorded — the operator needs that to recover by hand.
    assert client.charges == {"ik_1": 5000}


# ===========================================================================
# Law 5 — the gate cases: a no-compensator irreversible blocks commit (the
# honest "cannot undo" → human gate), and an explicit approval lets it through.
# ===========================================================================


@pytest.mark.parametrize("register, resource, kwargs, method", GATE_CASES)
def test_gate_blocks_without_approval(register, resource, kwargs, method):
    client = RecordingClient()
    action = register(client)

    with pytest.raises(GateBlocked):
        with agent_txn({resource: HTTPAdapter()}):
            action(**kwargs)

    # Gate blocked before the forward fold → the action never fired.
    assert client.calls.get(method, 0) == 0


@pytest.mark.parametrize("register, resource, kwargs, method", GATE_CASES)
def test_gate_fires_with_approval(register, resource, kwargs, method):
    from pherix.core.effects import StagedResult

    client = RecordingClient()
    action = register(client)

    with agent_txn({resource: HTTPAdapter()}) as ctx:
        staged = action(**kwargs)
        assert isinstance(staged, StagedResult)
        ctx.approve_irreversible(staged.effect_id)

    assert ctx.txn.state is TxnState.COMMITTED
    assert client.calls.get(method, 0) == 1
