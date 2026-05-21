"""#7 — world-state-aware commit-time policy (TOCTOU divergence).

The whole point: the policy is evaluated twice — stage-time and commit-time —
and a rule that reads *live* world state can return a different verdict at the
two evaluations because the world moved between them. An args-only rule can
never show this (the args are identical at both walks); a world-state rule can.

These tests construct :class:`PolicyContext` directly with a real SQLite
adapter (or a fake reader), so they prove the behaviour at the *policy layer*
without depending on the runtime wiring the orchestrator adds. They are written
to FAIL against main, where ``PolicyContext.read`` raises NotImplementedError.
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.effects import Effect
from pherix.core.policy import (
    Allow,
    Deny,
    Policy,
    PolicyContext,
    PolicyViolation,
    refund_if_paid,
    sql_reader,
)


def _effect(tool: str, args: dict, *, index: int = 0) -> Effect:
    return Effect(
        txn_id="txn-ws",
        index=index,
        tool=tool,
        args=args,
        resource="sql",
        reversible=True,
    )


@pytest.fixture
def orders_conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
    )
    c.execute("INSERT INTO orders (id, status) VALUES (42, 'paid')")
    yield c
    c.close()


# --- the reader seam --------------------------------------------------------


def test_read_without_bound_reader_raises_clear_error():
    ctx = PolicyContext(journal=[], where="stage")
    with pytest.raises(RuntimeError, match="no read mediator is bound"):
        ctx.read("sql", ("orders", "id", 42, "status"))


def test_sql_reader_returns_live_value(orders_conn):
    adapter = SQLiteAdapter(orders_conn)
    reader = sql_reader({"sql": adapter})
    ctx = PolicyContext(journal=[], where="stage", reader=reader)
    assert ctx.read("sql", ("orders", "id", 42, "status")) == "paid"


def test_sql_reader_absent_row_returns_none(orders_conn):
    adapter = SQLiteAdapter(orders_conn)
    ctx = PolicyContext(
        journal=[], where="stage", reader=sql_reader({"sql": adapter})
    )
    assert ctx.read("sql", ("orders", "id", 999, "status")) is None


# --- the divergence: PASS at stage, DENY at commit --------------------------


def test_refund_if_paid_diverges_when_world_moves(orders_conn):
    """The load-bearing test. Same rule, same effect, same args — yet Allow at
    stage-time and Deny at commit-time, purely because the order's live status
    flipped between the two evaluations."""
    adapter = SQLiteAdapter(orders_conn)
    reader = sql_reader({"sql": adapter})
    rule = refund_if_paid()  # canonical: refund_order on orders.status

    effect = _effect("refund_order", {"order_id": 42})

    # Stage-time: order is 'paid' → the rule permits.
    stage_ctx = PolicyContext(journal=[], where="stage", reader=reader)
    assert isinstance(rule(effect, stage_ctx), Allow)

    # The world moves between the two evaluations (a concurrent actor refunds
    # the order, or the order is cancelled — anything that leaves it non-paid).
    orders_conn.execute("UPDATE orders SET status = 'refunded' WHERE id = 42")

    # Commit-time: order is no longer 'paid' → the SAME rule now denies.
    commit_ctx = PolicyContext(journal=[effect], where="commit", reader=reader)
    verdict = rule(effect, commit_ctx)
    assert isinstance(verdict, Deny)
    assert "not 'paid'" in verdict.reason
    assert "refunded" in verdict.reason


def test_refund_if_paid_allows_at_both_when_world_stable(orders_conn):
    adapter = SQLiteAdapter(orders_conn)
    reader = sql_reader({"sql": adapter})
    rule = refund_if_paid()
    effect = _effect("refund_order", {"order_id": 42})

    stage_ctx = PolicyContext(journal=[], where="stage", reader=reader)
    commit_ctx = PolicyContext(journal=[effect], where="commit", reader=reader)
    assert isinstance(rule(effect, stage_ctx), Allow)
    assert isinstance(rule(effect, commit_ctx), Allow)


def test_refund_if_paid_ignores_other_tools(orders_conn):
    reader = sql_reader({"sql": SQLiteAdapter(orders_conn)})
    rule = refund_if_paid()
    # A different tool is a no-op Allow — the rule never even reads.
    effect = _effect("update_user", {"user_id": 1})
    ctx = PolicyContext(journal=[], where="stage", reader=reader)
    assert isinstance(rule(effect, ctx), Allow)


def test_refund_if_paid_denies_missing_id_arg(orders_conn):
    reader = sql_reader({"sql": SQLiteAdapter(orders_conn)})
    rule = refund_if_paid()
    effect = _effect("refund_order", {})  # no order_id
    ctx = PolicyContext(journal=[], where="stage", reader=reader)
    verdict = rule(effect, ctx)
    assert isinstance(verdict, Deny)


# --- the divergence through Policy.evaluate / evaluate_journal --------------


def test_divergence_through_policy_evaluate_journal(orders_conn):
    """Same divergence, but driven through the real Policy entry points
    (``evaluate`` at stage, ``evaluate_journal`` at commit) the runtime uses —
    proving the bracket, not just the bare rule, flips verdict."""
    adapter = SQLiteAdapter(orders_conn)
    reader = sql_reader({"sql": adapter})
    policy = Policy.with_rules(rules=[refund_if_paid()])

    effect = _effect("refund_order", {"order_id": 42})

    # Stage-time walk passes (the runtime would journal the effect).
    journal: list[Effect] = []
    ctx = PolicyContext(journal=journal, where="stage", reader=reader)
    policy.evaluate(effect, ctx, where="stage")  # no raise
    journal.append(effect)

    # World moves.
    orders_conn.execute("UPDATE orders SET status = 'refunded' WHERE id = 42")

    # Commit-time re-walk denies.
    class _Txn:
        effects = journal

    with pytest.raises(PolicyViolation) as ei:
        policy.evaluate_journal(_Txn(), ctx)
    assert ei.value.where == "commit"
    assert "refund_if_paid" in (ei.value.rule.name or "")


def test_fake_reader_proves_layer_independence():
    """``ctx.read`` works against any callable, not just SQL — so a rule can be
    unit-tested with a one-line fake. Proves the mediator is a clean seam."""
    box = {"status": "paid"}
    reader = lambda resource, key: box["status"]  # noqa: E731 — terse fake
    rule = refund_if_paid()
    effect = _effect("refund_order", {"order_id": 7})

    ctx = PolicyContext(journal=[], where="stage", reader=reader)
    assert isinstance(rule(effect, ctx), Allow)

    box["status"] = "cancelled"
    ctx2 = PolicyContext(journal=[], where="commit", reader=reader)
    assert isinstance(rule(effect, ctx2), Deny)
