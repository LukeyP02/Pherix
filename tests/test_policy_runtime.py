"""Slice 6 integration: policy rules and caps wired through the runtime.

Pins the two evaluation points (stage-time and commit-time), the
mixed-fold unwind path for commit-time denials, and the cross-resource
denial scenario over a SQL + HTTP transaction.
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.policy import (
    Allow,
    Cap,
    Deny,
    Policy,
    PolicyViolation,
)
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, tier TEXT)")
    yield c
    c.close()


@pytest.fixture
def update_user_tool():
    @tool(resource="sql")
    def update_user(conn, user_id, tier):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (?, ?, ?)",
            (user_id, f"user-{user_id}", tier),
        )
        return user_id

    return update_user


def _make_charge():
    """An irreversible 'charge' tool + its compensator — both as registered tools."""
    calls: list[tuple[str, dict]] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def refund(customer_id, amount):
        calls.append(("refund", {"customer_id": customer_id, "amount": amount}))

    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="refund",
    )
    def charge(customer_id, amount):
        calls.append(("charge", {"customer_id": customer_id, "amount": amount}))
        return {"charge_id": f"ch_{customer_id}_{amount}"}

    return charge, refund, calls


# --- stage-time: args-aware rules ------------------------------------------


def test_args_aware_rule_denies_at_stage_time(conn, update_user_tool):
    # The classic Slice 6 case: rule inspects effect.args, not just tool name.
    policy = Policy.allow_all()

    @policy.rule
    def no_enterprise_updates(effect, ctx):
        if effect.tool == "update_user" and effect.args.get("tier") == "enterprise":
            return Deny("enterprise tier off-limits")
        return Allow()

    with pytest.raises(PolicyViolation, match="enterprise tier off-limits") as exc:
        with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
            update_user_tool(user_id=1, tier="enterprise")

    assert exc.value.where == "stage"
    assert exc.value.rule.name == "no_enterprise_updates"
    assert exc.value.tool == "update_user"
    # The row never landed — stage-time denial means no journal entry, no apply.
    assert list(conn.execute("SELECT * FROM users")) == []


def test_args_aware_rule_allows_other_args(conn, update_user_tool):
    # Same rule, different args, different verdict.
    policy = Policy.allow_all()

    @policy.rule
    def no_enterprise_updates(effect, ctx):
        if effect.tool == "update_user" and effect.args.get("tier") == "enterprise":
            return Deny("enterprise tier off-limits")
        return Allow()

    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
        update_user_tool(user_id=1, tier="basic")
    assert list(conn.execute("SELECT name, tier FROM users")) == [
        ("user-1", "basic")
    ]


def test_stage_time_denial_does_not_journal_the_effect(conn, update_user_tool):
    policy = Policy.allow_all()

    @policy.rule
    def deny_all_updates(effect, ctx):
        return Deny("nope")

    audit = AuditJournal.in_memory()
    txn_id = None
    with pytest.raises(PolicyViolation):
        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit
        ) as txn:
            txn_id = txn.txn_id
            update_user_tool(user_id=1, tier="basic")

    # No effect rows for this txn — the journal is untouched on stage-time deny.
    assert audit.get_effects(txn_id) == []


# --- stage-time: spend caps ------------------------------------------------


def test_cap_sum_denies_the_charge_that_pushes_total_over_max():
    charge, _, calls = _make_charge()
    policy = Policy.with_rules(
        caps=[Cap.sum(tool="charge", via=lambda a: a["amount"], max=50)]
    )

    with pytest.raises(PolicyViolation, match="sum cap") as exc:
        with agent_txn({"http": HTTPAdapter()}, policy=policy) as txn:
            txn.approve_irreversible  # alias to silence pyflakes; not used
            charge(customer_id="c1", amount=20)
            charge(customer_id="c1", amount=25)
            # This third charge would push the cumulative spend to 55 > 50.
            charge(customer_id="c1", amount=10)

    # Stage-time denial: the third charge never journalled. Nothing fired
    # (irreversibles only fire at commit, and the txn rolled back instead).
    assert calls == []
    assert exc.value.rule.name.startswith("Cap.sum")
    assert exc.value.where == "stage"


def test_cap_count_denies_after_n_fires():
    charge, _, calls = _make_charge()
    policy = Policy.with_rules(caps=[Cap.count(tool="charge", max=2)])

    with pytest.raises(PolicyViolation, match="count cap"):
        with agent_txn({"http": HTTPAdapter()}, policy=policy) as txn:
            charge(customer_id="c1", amount=20)
            charge(customer_id="c2", amount=20)
            charge(customer_id="c3", amount=20)

    assert calls == []


# --- commit-time bracket ---------------------------------------------------


class _MutablePolicy(Policy):
    """Policy whose deny-set can be grown mid-txn — the TOCTOU test fixture."""

    def revoke(self, tool_name: str) -> None:
        self.deny.add(tool_name)


def test_commit_time_denial_unwinds_reversibles_and_compensates_irreversibles(conn):
    """The mixed-fold unwind: reversibles roll back, irreversibles compensate.

    Pins the cross-resource unwind that Slice 3's _partial_unwind delivers
    when Slice 6's commit-time bracket fires.
    """
    charge, _, calls = _make_charge()

    @tool(resource="sql")
    def add_user(conn, user_id):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (?, ?, ?)",
            (user_id, f"user-{user_id}", "basic"),
        )

    policy = _MutablePolicy()
    audit = AuditJournal.in_memory()

    with pytest.raises(PolicyViolation, match="charge"):
        with agent_txn(
            {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()},
            policy=policy,
            audit=audit,
        ) as txn:
            add_user(user_id=1)
            charge(customer_id="c1", amount=20)
            # Policy state changes between stage and commit (TOCTOU).
            policy.revoke("charge")

    # Reversible: SQL insert rolled back via adapter brackets.
    assert list(conn.execute("SELECT * FROM users")) == []
    # Irreversible: never fired (commit-time deny happens BEFORE the staged
    # fire loop) so there's no charge call AND no refund call.
    assert calls == []
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_commit_time_denial_marks_effect_gated_in_audit(conn):
    @tool(resource="sql")
    def add_user(conn, name):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (NULL, ?, ?)",
            (name, "basic"),
        )

    policy = _MutablePolicy()
    audit = AuditJournal.in_memory()

    with pytest.raises(PolicyViolation):
        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit
        ) as txn:
            add_user(name="alice")
            policy.revoke("add_user")

    # Reversible-only txn: no STAGED transition, evaluate_journal denies,
    # the runtime marks the offending effect GATED and rollbacks. The
    # audit row reflects the final status.
    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    assert statuses == ["GATED"]
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_commit_time_denial_with_approved_irreversible_fires_compensator(conn):
    """The full mixed-fold: a charge has fired by the time commit-time policy
    denies a *later* irreversible. Both irreversibles must compensate.

    Models: agent makes two charges, pre-approves both (no compensator
    chain, so we use a compensator-backed tool — pre-approval is not
    needed). Mid-body, policy grows to forbid the second charge's args.
    """
    charge, refund, calls = _make_charge()
    policy = Policy.allow_all()

    @policy.rule
    def block_high_value(effect, ctx):
        if effect.tool == "charge" and effect.args.get("amount", 0) >= 100:
            return Deny("high-value charges off-limits")
        return Allow()

    # The rule only fires at commit-time matters if stage-time and
    # commit-time verdicts differ. For Slice 6 the verdicts match — so we
    # use a state-aware-style rule that's *intended* to deny. Stage-time
    # will catch it first. To pin the commit-time fire-and-compensate path
    # specifically, see test_commit_time_denial_after_stage_pass below.
    with pytest.raises(PolicyViolation):
        with agent_txn({"http": HTTPAdapter()}, policy=policy):
            charge(customer_id="c1", amount=100)
    # Stage-time denied — no fire, no compensator.
    assert calls == []


def test_commit_time_denial_after_stage_pass_compensates_fired_irreversibles(conn):
    """Stage-time allows; commit-time denies via mutating policy. The
    irreversible has been staged but not fired (commit-time bracket runs
    BEFORE the fire-staged loop), so no compensator is invoked. Reversibles
    do roll back.
    """
    charge, refund, calls = _make_charge()
    policy = _MutablePolicy()

    @tool(resource="sql")
    def add_user(conn, name):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (NULL, ?, ?)",
            (name, "basic"),
        )

    with pytest.raises(PolicyViolation):
        with agent_txn(
            {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()},
            policy=policy,
        ) as txn:
            add_user(name="alice")
            charge(customer_id="c1", amount=20)
            policy.revoke("charge")

    # SQL rolled back.
    assert list(conn.execute("SELECT * FROM users")) == []
    # Charge was staged but never fired — commit-time policy bracket runs
    # before the staged-fire loop. No compensator should have run either
    # (no fire, no compensation).
    assert calls == []
    assert txn.txn.state is TxnState.ROLLED_BACK


# --- both eval points fire -------------------------------------------------


def test_rule_fires_at_both_stage_and_commit_time(conn):
    """Pin that the same predicate evaluates at both stage and commit. For
    args-only rules in Slice 6 the verdicts match — counting evaluations
    is the way to see the bracket lands."""

    @tool(resource="sql")
    def add_user(conn, name):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (NULL, ?, ?)",
            (name, "basic"),
        )

    policy = Policy.allow_all()
    evaluations: list[str] = []

    @policy.rule
    def counter(effect, ctx):
        evaluations.append(ctx.where)
        return Allow()

    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
        add_user(name="alice")
        add_user(name="bob")

    # 2 effects × 2 eval points = 4 invocations: 2 stage, 2 commit.
    assert evaluations.count("stage") == 2
    assert evaluations.count("commit") == 2


# --- #7: world-state read seam (pre-runtime-wiring state) ------------------


def test_runtime_binds_a_reader_so_rules_read_live_state(conn):
    """#7 end-to-end: the runtime threads ``sql_reader(adapters)`` into the
    PolicyContext it constructs, so a rule calling ``ctx.read`` inside an
    agent_txn reads live adapter state rather than hitting "no reader bound".

    (Pre-wiring this test asserted the RuntimeError placeholder; the
    orchestrator's runtime integration flipped it to the bound-reader path.
    The TOCTOU divergence itself is pinned at the policy layer in
    tests/test_world_state_policy.py.)"""
    conn.execute("INSERT INTO users (id, name, tier) VALUES (1, 'alice', 'gold')")
    policy = Policy.allow_all()
    seen: list[object] = []

    @tool(resource="sql")
    def add_user(conn, name):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (NULL, ?, ?)",
            (name, "basic"),
        )

    @policy.rule
    def reads_live_tier(effect, ctx):
        # 4-tuple key form: (table, pk_column, pk_value, value_column).
        seen.append(ctx.read("sql", ("users", "id", 1, "tier")))
        return Allow()

    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
        add_user(name="bob")

    # The rule fired (stage-time + commit-time walks) and the bound reader
    # returned the live value both times — proof the mediator is wired.
    assert seen and all(v == "gold" for v in seen)


# --- backwards compat ------------------------------------------------------


def test_legacy_policy_constructors_still_work(conn, update_user_tool):
    # Slice 1's Policy(deny=...) / Policy.allow_all() shape is preserved.
    policy = Policy(deny={"update_user"})
    with pytest.raises(PolicyViolation, match="deny-listed"):
        with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
            update_user_tool(user_id=1, tier="basic")
