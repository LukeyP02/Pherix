"""Property-based laws of policy enforcement.

The kernel claim under test: **a denied effect never touches a resource, and a
denial unwinds the whole transaction to its committed baseline — no partial
application.** Policy is evaluated at stage-time *before* the adapter applies a
reversible effect, so a denied effect cannot leave even a partial write; and
because the denial aborts the ``with`` block, every *prior* effect is rolled
back too. We assert this over arbitrary programs, branching the expectation on
whether the generated program trips the rule.

Two rule shapes are exercised over random input: a predicate :class:`Deny`
(reject any negative write) and a count :class:`Cap` (at most ``K`` writes per
txn). The law is identical for both — deny ⇒ world unchanged.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.policy import Allow, Cap, Deny, Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

from tests._laws import dump_kv, fresh_kv_conn, kv_programs, seed_programs

# Trust pillars: oversight (policy is the human-authored gate — a denied effect
# is refused) and blast radius (a denial unwinds the whole txn to baseline, no
# partial application).
pytestmark = [pytest.mark.oversight, pytest.mark.blast_radius]

_LAW = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def kv_tools():
    @tool(resource="sql")
    def kv_set(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        return v

    @tool(resource="sql")
    def kv_del(conn, k):
        conn.execute("DELETE FROM kv WHERE k = ?", (k,))

    return kv_set, kv_del


def _run(tools, prog):
    kv_set, kv_del = tools
    for op in prog:
        if op.op == "set":
            kv_set(k=op.key, v=op.value)
        else:
            kv_del(k=op.key)


def _reference(state, prog):
    out = dict(state)
    for op in prog:
        if op.op == "set":
            out[op.key] = op.value
        else:
            out.pop(op.key, None)
    return out


def _seed(conn, seed):
    for k, v in seed.items():
        conn.execute("INSERT INTO kv (k, v) VALUES (?, ?)", (k, v))


@given(seed=seed_programs(), prog=kv_programs())
@_LAW
def test_predicate_deny_leaves_world_untouched(kv_tools, seed, prog):
    """A predicate Deny aborts the whole txn — no partial application."""

    def no_negative(effect, ctx):
        if effect.tool == "kv_set" and effect.args.get("v", 0) < 0:
            return Deny("negative writes are forbidden")
        return Allow()

    policy = Policy.with_rules(rules=[no_negative])
    will_deny = any(op.op == "set" and op.value < 0 for op in prog)

    conn = fresh_kv_conn()
    try:
        _seed(conn, seed)
        before = dump_kv(conn)
        if will_deny:
            with pytest.raises(PolicyViolation):
                with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy) as txn:
                    _run(kv_tools, prog)
            # The denial rolled the whole txn back to the committed baseline.
            assert dump_kv(conn) == before
            assert txn.txn.state is TxnState.ROLLED_BACK
        else:
            with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy) as txn:
                _run(kv_tools, prog)
            assert dump_kv(conn) == _reference(before, prog)
            assert txn.txn.state is TxnState.COMMITTED
    finally:
        conn.close()


@given(seed=seed_programs(), prog=kv_programs())
@_LAW
def test_count_cap_is_all_or_nothing(kv_tools, seed, prog):
    """A count Cap on writes: exceeding it denies and unwinds completely.

    Either the program stays within the cap and commits in full, or it trips
    the cap on the (K+1)-th write and the whole txn rolls back — never a
    truncated prefix of K writes left committed.
    """
    K = 3
    policy = Policy(caps=[Cap.count(tool="kv_set", max=K)])
    n_sets = sum(1 for op in prog if op.op == "set")

    conn = fresh_kv_conn()
    try:
        _seed(conn, seed)
        before = dump_kv(conn)
        if n_sets > K:
            with pytest.raises(PolicyViolation):
                with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy) as txn:
                    _run(kv_tools, prog)
            assert dump_kv(conn) == before
            assert txn.txn.state is TxnState.ROLLED_BACK
        else:
            with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy) as txn:
                _run(kv_tools, prog)
            assert dump_kv(conn) == _reference(before, prog)
    finally:
        conn.close()
