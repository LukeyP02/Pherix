"""Slice 6 dogfood — the policy engine as a predicate fold over the journal.

Run:  python examples/slice6_demo.py

Three scenarios, each one a different way the engine reads the journal:

  1. Args-aware rule. The predicate inspects ``effect.args``, not the
     tool name alone. Same tool, different args, different verdict.

  2. Spend cap. ``Cap.sum`` folds the journal so far — the cap's
     running total is just a left-fold accumulator. The third charge
     trips the cap because adding its contribution would push the
     running sum above the configured max.

  3. Commit-time bracket on a mixed SQL+HTTP transaction. The policy's
     deny-set grows between stage-time and commit-time (a TOCTOU
     scenario). At commit-start, ``evaluate_journal`` re-walks the
     journal and denies. The SQL insert rolls back via the adapter's
     savepoint; the staged HTTP charge never fires (the bracket runs
     BEFORE the staged-fire loop — the strongest containment property
     Pherix offers for irreversible effects).

Maths framing: a Slice 6 policy is a predicate
``P : (Effect, Context) -> {Allow, Deny}`` lifted to a fold over the
journal. Caps make the fold visibly stateful — the cap's running total
is the accumulator carried across steps. Stage and commit are two
evaluation points of the same predicate, separated in time by the
journal-recording window — the TOCTOU gap.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Make the example runnable as `python examples/slice6_demo.py` without an
# editable install — put the repo root on the path before importing pherix.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (
    Allow,
    Cap,
    Deny,
    HTTPAdapter,
    Policy,
    PolicyViolation,
    SQLiteAdapter,
    agent_txn,
    tool,
)
from pherix.core.tools import REGISTRY


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def scenario_1_args_aware_rule() -> None:
    _banner("1. Args-aware rule — same tool, different verdicts")
    REGISTRY.clear()

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, tier TEXT)"
    )

    @tool(resource="sql")
    def update_user(conn, user_id, tier):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (?, ?, ?)",
            (user_id, f"user-{user_id}", tier),
        )

    policy = Policy.allow_all()

    @policy.rule
    def no_enterprise(effect, ctx):
        if effect.tool == "update_user" and effect.args.get("tier") == "enterprise":
            return Deny("enterprise tier off-limits")
        return Allow()

    # Basic tier — passes.
    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
        update_user(user_id=1, tier="basic")
    print("  update(tier='basic')      -> committed")

    # Enterprise tier — same tool, denied by the same rule.
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy):
            update_user(user_id=2, tier="enterprise")
    except PolicyViolation as exc:
        print(f"  update(tier='enterprise') -> {exc.__class__.__name__}")
        print(f"      where         = {exc.where!r}")
        print(f"      rule          = {exc.rule.name}")
        print(f"      reason        = {exc.reason!r}")
        print(f"      effect_index  = {exc.effect_index}")

    rows = list(conn.execute("SELECT id, tier FROM users"))
    print(f"  table after txns: {rows}")
    conn.close()


def scenario_2_spend_cap() -> None:
    _banner("2. Cap.sum — the third charge trips the cumulative cap")
    REGISTRY.clear()

    calls: list[dict] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def charge(customer_id, amount):
        calls.append({"customer_id": customer_id, "amount": amount})

    policy = Policy.with_rules(
        caps=[Cap.sum(tool="charge", via=lambda args: args["amount"], max=50)]
    )

    try:
        with agent_txn({"http": HTTPAdapter()}, policy=policy):
            charge(customer_id="c1", amount=20)  # running = 20  ok
            charge(customer_id="c1", amount=25)  # running = 45  ok
            charge(customer_id="c1", amount=10)  # would be 55 > 50  DENY
    except PolicyViolation as exc:
        print(f"  third charge denied -> {exc.__class__.__name__}")
        print(f"      rule          = {exc.rule.name}")
        print(f"      reason        = {exc.reason!r}")
        print(f"      where         = {exc.where!r}")

    # Irreversibles only fire at commit. Stage-time denial means the txn
    # auto-rolled back — no charges fired at all (the strongest containment
    # property for irreversibles).
    print(f"  charges fired: {calls}  (none — stage-time denial preempts commit)")


def scenario_3_commit_time_unwind_mixed_txn() -> None:
    _banner("3. Commit-time bracket — TOCTOU on a mixed SQL+HTTP txn")
    REGISTRY.clear()

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, tier TEXT)"
    )

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
        return {"charge_id": "ch_xyz"}

    @tool(resource="sql")
    def add_user(conn, name):
        conn.execute(
            "INSERT INTO users (id, name, tier) VALUES (NULL, ?, ?)",
            (name, "basic"),
        )

    class MutablePolicy(Policy):
        """Policy whose deny-set can grow between stage and commit."""

        def revoke(self, tool_name):
            self.deny.add(tool_name)

    policy = MutablePolicy()

    try:
        with agent_txn(
            {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()},
            policy=policy,
        ) as txn:
            add_user(name="alice")  # reversible, APPLIED live
            charge(customer_id="c1", amount=20)  # irreversible, STAGED
            policy.revoke("charge")  # TOCTOU — world shifts
    except PolicyViolation as exc:
        print(f"  commit-time deny -> {exc.__class__.__name__}")
        print(f"      where         = {exc.where!r}")
        print(f"      effect_index  = {exc.effect_index}")
        print(f"      reason        = {exc.reason!r}")
        print(f"      txn state     = {txn.txn.state.name}")

    rows = list(conn.execute("SELECT id, name FROM users"))
    print(f"  SQL state after txn: {rows}  (insert rolled back via savepoint)")
    print(f"  HTTP fires:          {calls}  (charge never fired — bracket preempts)")
    conn.close()


if __name__ == "__main__":
    scenario_1_args_aware_rule()
    scenario_2_spend_cap()
    scenario_3_commit_time_unwind_mixed_txn()
    print()
    print("Slice 6 demo done.")
