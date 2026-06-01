"""Authority policy — actor trust tiers over the existing stage+commit eval.

PR #40 gave every effect an ``actor`` (on-whose-authority), and the policy fold
already reaches it (``tests/test_actor.py::test_policy_rule_can_branch_on_actor``
is the passing proof of the raw capability). What was missing is the *product
surface*: a declarative way to express authority over that actor. This is the
Policy axis extended — :class:`TrustTiers` + three vetted factories
(:func:`untrusted_gates_irreversible`, :func:`admin_auto_approves`,
:func:`per_actor_count_cap`) — a thin layer over the same twice-evaluated fold,
plus one additive gate seam (:attr:`Policy.auto_approve`).

These tests drive a real transaction through stage + commit and pin three
behaviours that fire *only because of* the actor-aware primitive:

- **forced gate** — an untrusted actor's irreversible effect is denied (never
  fires), where the identical effect from a trusted actor commits.
- **auto-approve** — an admin's gated irreversible commits with NO
  ``approve_irreversible`` call, where a non-admin's identical effect blocks.
- **per-actor cap** — an actor's N+1-th effect is denied while another actor's
  effects flow freely.

Each is a genuine red against ``origin/main``: there is no ``TrustTiers``,
``untrusted_gates_irreversible``, ``admin_auto_approves``, or
``per_actor_count_cap`` there, and ``Policy`` has no ``auto_approve`` seam — so
the imports alone fail before this change, and the *behaviour* (not just the
symbol) is what each assertion turns on. The denials/auto-approvals are NOT
tautological: each is paired with a control actor for whom the rule does NOT
fire, driven through the same engine.

Tools are defined *inside* each test: the autouse ``_clean_tool_registry``
fixture clears the process-global registry around every test (same reason as
``test_actor.py``).
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.effects import Effect
from pherix.core.policy import (
    Allow,
    Policy,
    PolicyContext,
    PolicyViolation,
    TrustTiers,
    admin_auto_approves,
    per_actor_count_cap,
    untrusted_gates_irreversible,
)
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import acting_as, tool
from pherix.core.transaction import TxnState

# Trust pillar: oversight — authority over the actor governs the commit-time
# gate (forced for untrusted, auto-cleared for admin) and the per-actor cap.
pytestmark = pytest.mark.oversight


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def _register_insert():
    @tool(resource="sql", reversible=True)
    def insert_widget(conn: sqlite3.Connection, name: str) -> None:
        conn.execute("INSERT INTO widgets (name) VALUES (?)", (name,))

    return insert_widget


def _register_send_email():
    """An irreversible (HTTP) tool with no compensator — needs a gate-pass."""
    calls: list[dict] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, body):
        calls.append({"to": to, "body": body})

    return send_email, calls


# --- TrustTiers resolution (incl. NULL-tolerance) --------------------------


def test_trust_tiers_resolves_each_set_and_null_default() -> None:
    tiers = TrustTiers(
        admin=["root"], trusted=["alice"], untrusted=["mallory"]
    )
    assert tiers.tier_of("root") == "admin"
    assert tiers.tier_of("alice") == "trusted"
    assert tiers.tier_of("mallory") == "untrusted"
    # NULL actor → the safe default (untrusted), not a crash.
    assert tiers.tier_of(None) == "untrusted"
    # named-but-unlisted → unknown_tier (untrusted by default).
    assert tiers.tier_of("stranger") == "untrusted"
    # the authority ladder is ordered.
    assert tiers.rank_of("root") > tiers.rank_of("alice") > tiers.rank_of(
        "mallory"
    )
    assert tiers.at_least("alice", "trusted") is True
    assert tiers.at_least("mallory", "trusted") is False


def test_trust_tiers_rejects_overlapping_membership() -> None:
    with pytest.raises(ValueError, match="more than one tier"):
        TrustTiers(admin=["x"], trusted=["x"])


def test_trust_tiers_null_default_can_be_lifted() -> None:
    """An operator may choose to trust the unattributed effect by default."""
    tiers = TrustTiers(admin=["root"], default_tier="trusted")
    assert tiers.tier_of(None) == "trusted"
    assert tiers.at_least(None, "trusted") is True


# --- RULE 1: untrusted actor → forced gate on irreversibles ----------------


def test_untrusted_actor_irreversible_is_gated_trusted_commits(conn) -> None:
    """The forced gate fires ONLY because of the actor-aware rule.

    Drive the SAME irreversible effect through the engine twice: once as a
    trusted actor (commits) and once as an untrusted actor (denied, never
    fires). The divergence is attributable purely to ``effect.actor``.
    """
    tiers = TrustTiers(trusted=["alice"], untrusted=["mallory"])
    policy = Policy.with_rules(
        rules=[untrusted_gates_irreversible(tiers)],
    )
    send, calls = _register_send_email()

    # trusted actor: a compensator-less irreversible would normally hit the
    # human gate, but here we approve it out-of-band — the rule does NOT block.
    with agent_txn(
        {"http": HTTPAdapter()}, policy=policy, actor="alice"
    ) as txn:
        r = send(to="a@x.com", body="hi")
        txn.approve_irreversible(r.effect_id)
    assert calls == [{"to": "a@x.com", "body": "hi"}]
    assert txn.txn.state is TxnState.COMMITTED

    # untrusted actor: the rule DENIES at the gate — even an out-of-band
    # approval cannot save it, because the deny fires before the gate is
    # reached (stage-time) and again at commit-time.
    with pytest.raises(PolicyViolation, match="mallory"):
        with agent_txn(
            {"http": HTTPAdapter()}, policy=policy, actor="mallory"
        ) as txn2:
            r2 = send(to="b@x.com", body="nope")
            # Even approving it does not help: the rule denies, not the gate.
            txn2.approve_irreversible(r2.effect_id)
    assert calls == [{"to": "a@x.com", "body": "hi"}]  # untrusted never fired


def test_untrusted_gate_does_not_touch_reversibles(conn) -> None:
    """The forced gate is scoped to the irreversible lane only.

    An untrusted actor's *reversible* effect (clean state-rollback exists) is
    NOT gated by this rule — the rule is a no-op Allow for reversibles.
    """
    tiers = TrustTiers(trusted=["alice"], untrusted=["mallory"])
    policy = Policy.with_rules(rules=[untrusted_gates_irreversible(tiers)])
    insert_widget = _register_insert()
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, policy=policy, actor="mallory"
    ) as txn:
        insert_widget(name="reversible-is-fine")
    assert txn.txn.state is TxnState.COMMITTED
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 1


def test_null_actor_irreversible_is_gated(conn) -> None:
    """NULL-tolerance: an unattributed irreversible is gated (default=untrusted)."""
    tiers = TrustTiers(trusted=["alice"])
    policy = Policy.with_rules(rules=[untrusted_gates_irreversible(tiers)])
    send, calls = _register_send_email()
    with pytest.raises(PolicyViolation, match="None"):
        with agent_txn({"http": HTTPAdapter()}, policy=policy) as txn:
            send(to="x@x.com", body="anon")
    assert calls == []


# --- RULE 2: admin actor → auto-approve the gate ---------------------------


def test_admin_auto_approves_gate_non_admin_blocks() -> None:
    """Auto-approval fires ONLY because of the actor-aware predicate.

    A compensator-less irreversible normally GateBlocks with no out-of-band
    approval. With ``admin_auto_approves`` on the policy, an *admin* actor's
    effect commits with NO ``approve_irreversible`` call — the authority IS the
    approval — while a non-admin's identical effect still blocks.
    """
    tiers = TrustTiers(admin=["root"], trusted=["alice"])
    policy = Policy.with_rules(auto_approve=admin_auto_approves(tiers))
    send, calls = _register_send_email()

    # admin: commits WITHOUT any approve_irreversible call.
    with agent_txn(
        {"http": HTTPAdapter()}, policy=policy, actor="root"
    ) as txn:
        send(to="ops@x.com", body="deploy")
        # NOTE: no txn.approve_irreversible(...) — the admin authority clears it.
    assert calls == [{"to": "ops@x.com", "body": "deploy"}]
    assert txn.txn.state is TxnState.COMMITTED

    # non-admin (trusted, but below admin): the gate still blocks.
    with pytest.raises(GateBlocked):
        with agent_txn(
            {"http": HTTPAdapter()}, policy=policy, actor="alice"
        ):
            send(to="user@x.com", body="please")
    # the non-admin email never fired — calls unchanged from the admin commit.
    assert calls == [{"to": "ops@x.com", "body": "deploy"}]


def test_admin_auto_approve_does_not_leak_to_null_actor() -> None:
    """NULL-tolerance: an unattributed effect is never auto-approved."""
    tiers = TrustTiers(admin=["root"])
    policy = Policy.with_rules(auto_approve=admin_auto_approves(tiers))
    send, calls = _register_send_email()
    with pytest.raises(GateBlocked):
        with agent_txn({"http": HTTPAdapter()}, policy=policy):
            send(to="x@x.com", body="anon")
    assert calls == []


def test_full_authority_ladder_compose(conn) -> None:
    """untrusted → forced gate, trusted → human gate, admin → auto-approved.

    Both factories on one policy, exercised through three real transactions.
    """
    tiers = TrustTiers(
        admin=["root"], trusted=["alice"], untrusted=["mallory"]
    )
    policy = Policy.with_rules(
        rules=[untrusted_gates_irreversible(tiers)],
        auto_approve=admin_auto_approves(tiers),
    )
    send, calls = _register_send_email()

    # untrusted → DENIED (forced gate via the deny rule).
    with pytest.raises(PolicyViolation, match="mallory"):
        with agent_txn(
            {"http": HTTPAdapter()}, policy=policy, actor="mallory"
        ):
            send(to="m@x.com", body="x")
    assert calls == []

    # trusted → passes the rule, but the human gate still requires approval.
    with pytest.raises(GateBlocked):
        with agent_txn(
            {"http": HTTPAdapter()}, policy=policy, actor="alice"
        ):
            send(to="a@x.com", body="y")
    assert calls == []  # still nothing fired

    # admin → passes the rule AND auto-clears the gate.
    with agent_txn(
        {"http": HTTPAdapter()}, policy=policy, actor="root"
    ) as txn:
        send(to="r@x.com", body="z")
    assert calls == [{"to": "r@x.com", "body": "z"}]
    assert txn.txn.state is TxnState.COMMITTED


# --- RULE 3: per-actor capability cap --------------------------------------


def test_per_actor_cap_denies_only_the_capped_actor(conn) -> None:
    """The cap fires ONLY for the over-budget actor; another actor flows free.

    Drive a real SQL transaction; alice has a cap of 2, bob is uncapped. Alice's
    third insert is denied (rolling the whole txn back — nothing commits);
    bob's three inserts in a separate txn all commit.
    """
    policy = Policy.with_rules(
        rules=[per_actor_count_cap(limits={"alice": 2}, default=None)],
    )
    insert_widget = _register_insert()

    # alice: the 3rd insert trips the cap → PolicyViolation → full rollback.
    with pytest.raises(PolicyViolation, match="alice"):
        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, policy=policy, actor="alice"
        ):
            insert_widget(name="a1")
            insert_widget(name="a2")
            insert_widget(name="a3")  # one too many for alice
    # Atomic: alice's whole txn rolled back, so NO rows landed.
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 0

    # bob (uncapped, default=None): three inserts all commit.
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, policy=policy, actor="bob"
    ) as txn:
        insert_widget(name="b1")
        insert_widget(name="b2")
        insert_widget(name="b3")
    assert txn.txn.state is TxnState.COMMITTED
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 3


def test_per_actor_cap_counts_per_actor_via_acting_as(conn) -> None:
    """The cap buckets by the per-call actor, not the txn default.

    With a default cap of 1, two effects under DIFFERENT ``acting_as`` actors
    both pass (each its own bucket), but a second effect under the SAME actor
    trips the cap. Proves the cap reads the per-effect stamped actor.
    """
    policy = Policy.with_rules(
        rules=[per_actor_count_cap(limits={}, default=1)],
    )
    insert_widget = _register_insert()
    # One each for alice and bob — both within their per-actor cap of 1.
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, policy=policy, actor="system"
    ) as txn:
        with acting_as("alice"):
            insert_widget(name="alice-1")
        with acting_as("bob"):
            insert_widget(name="bob-1")
    assert txn.txn.state is TxnState.COMMITTED
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 2

    # A second effect for alice in a fresh txn trips alice's cap of 1.
    with pytest.raises(PolicyViolation, match="alice"):
        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, policy=policy
        ):
            with acting_as("alice"):
                insert_widget(name="alice-2a")
                insert_widget(name="alice-2b")  # alice's 2nd → over the cap of 1


def test_per_actor_cap_agrees_at_stage_and_commit() -> None:
    """The journal-fold count gives the same verdict at stage and commit.

    Construct the policy context directly and evaluate a 3rd same-actor effect
    at BOTH where='stage' (candidate not yet in journal) and where='commit'
    (candidate already in journal). The cap of 2 must deny in both, proving the
    ``e.index != effect.index`` exclusion keeps the two walks in agreement.
    """
    rule = per_actor_count_cap(limits={"alice": 2})
    policy = Policy.with_rules(rules=[rule])

    def _eff(index: int) -> Effect:
        return Effect(
            txn_id="t",
            index=index,
            tool="do",
            args={},
            resource="http",
            reversible=False,
            actor="alice",
        )

    journal = [_eff(0), _eff(1), _eff(2)]

    # stage-time: the candidate (index 2) is NOT yet appended.
    stage_ctx = PolicyContext(journal=journal[:2], where="stage")
    with pytest.raises(PolicyViolation, match="alice"):
        policy.evaluate(journal[2], stage_ctx, where="stage")

    # commit-time: the candidate IS in the journal; same verdict.
    commit_ctx = PolicyContext(journal=journal, where="commit")
    with pytest.raises(PolicyViolation, match="alice"):
        policy.evaluate(journal[2], commit_ctx, where="commit")


# --- the seam itself: Policy.auto_approve is additive & default-off --------


def test_policy_auto_approve_defaults_off() -> None:
    """A policy with no auto_approve never auto-clears the gate (regression)."""
    policy = Policy.allow_all()
    e = Effect(
        txn_id="t", index=0, tool="x", args={}, resource="http",
        reversible=False, actor="root",
    )
    assert policy.approves_gate(e) is False


def test_rules_stay_callable_outside_pherix() -> None:
    """The factories return plain ``(effect, ctx) -> Verdict`` callables.

    Matching ``refund_if_paid`` / the ``@policy.rule`` contract — usable for a
    unit assertion without standing up the runtime.
    """
    tiers = TrustTiers(trusted=["alice"], untrusted=["mallory"])
    rule = untrusted_gates_irreversible(tiers)
    irreversible = Effect(
        txn_id="t", index=0, tool="x", args={}, resource="http",
        reversible=False, actor="alice",
    )
    assert isinstance(rule(irreversible, PolicyContext(journal=[], where="stage")), Allow)
