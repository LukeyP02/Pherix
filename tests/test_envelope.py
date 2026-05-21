"""#10 — longitudinal (durable, cross-run) envelope.

Per-txn caps reset between transactions; a longitudinal cap folds its
contribution over the cross-session journal and persists the running total to
disk, so the budget is shared across separate runs AND survives process
restart. These tests prove both, at the store/cap layer, without the runtime
wiring the orchestrator adds — they simulate the runtime's "flush on successful
commit" step explicitly via :func:`pending_increments` + :func:`flush_increments`.

Written to FAIL against main, where no durable store exists.
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect
from pherix.core.envelope import (
    DurableCap,
    EnvelopeStore,
    all_time_period,
    day_period,
    flush_increments,
    is_durable_cap,
    pending_increments,
)
from pherix.core.policy import (
    Allow,
    Deny,
    Policy,
    PolicyContext,
    PolicyViolation,
)


# --- helpers ----------------------------------------------------------------


def _charge(amount: float, *, index: int = 0) -> Effect:
    return Effect(
        txn_id=f"txn-{index}",
        index=index,
        tool="charge",
        args={"amount": amount},
        resource="http",
        reversible=False,
    )


def _simulate_commit(policy: Policy, journal: list[Effect]) -> None:
    """Stand in for the runtime's successful-commit flush.

    Folds the committed journal into durable-cap increments and applies them —
    exactly what the runtime's post-commit hook will do.
    """
    durable = [c for c in policy.caps if is_durable_cap(c)]
    flush_increments(pending_increments(durable, journal))


def _run_txn(policy: Policy, charges: list[float], *, commit: bool = True):
    """Evaluate a sequence of charges through the policy as one txn.

    Returns the journal of effects that passed the stage-time walk. On
    ``commit`` (default) flushes durable increments like the runtime would; on
    rollback (``commit=False``) it does NOT — proving a rolled-back txn spends
    nothing.
    """
    journal: list[Effect] = []
    ctx = PolicyContext(journal=journal, where="stage")
    for i, amt in enumerate(charges):
        eff = _charge(amt, index=i)
        policy.evaluate(eff, ctx, where="stage")  # raises on Deny
        journal.append(eff)
    if commit:
        _simulate_commit(policy, journal)
    return journal


# --- the store --------------------------------------------------------------


def test_store_total_absent_is_zero(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    assert store.total("any-cap", day_period()) == 0.0


def test_store_add_accumulates(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    period = day_period()
    assert store.add("cap", period, 10.0) == 10.0
    assert store.add("cap", period, 5.0) == 15.0
    assert store.total("cap", period) == 15.0


def test_store_periods_are_independent(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    store.add("cap", "2026-05-21", 10.0)
    store.add("cap", "2026-05-22", 3.0)
    assert store.total("cap", "2026-05-21") == 10.0
    assert store.total("cap", "2026-05-22") == 3.0


def test_store_lives_in_audit_journal_db(tmp_path):
    """Locked decision: the totals are a sibling table inside the audit DB,
    not a second .db file."""
    path = str(tmp_path / "audit.db")
    audit = AuditJournal(path)
    store = EnvelopeStore.from_audit(audit)
    store.add("cap", day_period(), 7.0)

    # The table is in the SAME database file the audit journal uses.
    names = {
        r[0]
        for r in audit._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "envelope_totals" in names
    assert "transactions" in names  # the audit journal's own table
    audit.close()

    # No sibling .db file was created.
    assert not (tmp_path / "audit.db-envelope").exists()


# --- (a) two runs in the SAME process share one budget ----------------------


def test_two_runs_same_process_share_count_budget(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    period = lambda: "fixed-period"  # noqa: E731 — pin the bucket for the test
    policy = Policy()
    policy.add_cap(
        DurableCap.count(tool="charge", max=3, store=store, period=period)
    )

    # Run 1: two charges, commits — consumes 2 of 3.
    _run_txn(policy, [1.0, 1.0])

    # Run 2: a brand-new PolicyContext (per-txn totals reset), but the durable
    # cap remembers run 1's spend. One charge is fine (total 3); a second
    # exceeds the shared budget.
    _run_txn(policy, [1.0])
    with pytest.raises(PolicyViolation) as ei:
        _run_txn(policy, [1.0])
    assert "durable count cap" in ei.value.reason


def test_two_runs_same_process_share_sum_budget(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    period = lambda: "fixed-period"  # noqa: E731
    policy = Policy()
    policy.add_cap(
        DurableCap.sum(
            tool="charge",
            via=lambda a: a["amount"],
            max=100.0,
            store=store,
            period=period,
        )
    )

    _run_txn(policy, [60.0])  # spent 60
    _run_txn(policy, [30.0])  # spent 90
    with pytest.raises(PolicyViolation) as ei:
        _run_txn(policy, [20.0])  # would be 110 > 100
    assert "durable sum cap" in ei.value.reason


def test_rolled_back_txn_does_not_consume_budget(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    period = lambda: "fixed-period"  # noqa: E731
    policy = Policy()
    policy.add_cap(
        DurableCap.sum(
            tool="charge",
            via=lambda a: a["amount"],
            max=100.0,
            store=store,
            period=period,
        )
    )
    # A run that does NOT commit (rolled back) must spend nothing.
    _run_txn(policy, [90.0], commit=False)
    assert store.total("DurableCap.sum(tool='charge', max=100.0)", "fixed-period") == 0.0
    # So a subsequent committed run still has the full budget.
    _run_txn(policy, [90.0])
    assert store.total("DurableCap.sum(tool='charge', max=100.0)", "fixed-period") == 90.0


def test_multiple_charges_in_one_run_accumulate_against_baseline(tmp_path):
    """Within ONE run, the journal-so-far fold must add on top of the persisted
    baseline — otherwise each fire would see the same stale total and a single
    run could blow the budget."""
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    period = lambda: "fixed-period"  # noqa: E731
    policy = Policy()
    policy.add_cap(
        DurableCap.count(tool="charge", max=2, store=store, period=period)
    )
    # First run consumes 1 of 2.
    _run_txn(policy, [1.0])
    # Second run: one charge is fine (total 2); two in the same run must trip
    # the cap on the second, seeing baseline(1) + this-txn-so-far(1).
    with pytest.raises(PolicyViolation):
        _run_txn(policy, [1.0, 1.0])


# --- (b) cross-restart: fresh handle on the same path sees prior spend ------


def test_fresh_store_handle_sees_prior_spend(tmp_path):
    path = str(tmp_path / "j.db")
    period_key = "fixed-period"

    # "Process 1": spend 2 charges, then close the handle (process death).
    store1 = EnvelopeStore.from_path(path)
    policy1 = Policy()
    policy1.add_cap(
        DurableCap.count(
            tool="charge", max=3, store=store1, period=lambda: period_key
        )
    )
    _run_txn(policy1, [1.0, 1.0])
    store1.close()

    # "Process 2": a brand-new store handle on the SAME path. It must observe
    # the 2 prior charges — so only one more is allowed before the cap bites.
    store2 = EnvelopeStore.from_path(path)
    assert store2.total(
        "DurableCap.count(tool='charge', max=3)", period_key
    ) == 2.0

    policy2 = Policy()
    policy2.add_cap(
        DurableCap.count(
            tool="charge", max=3, store=store2, period=lambda: period_key
        )
    )
    _run_txn(policy2, [1.0])  # total 3 — exactly at cap
    with pytest.raises(PolicyViolation):
        _run_txn(policy2, [1.0])  # would be 4 > 3
    store2.close()


def test_fresh_store_handle_sees_prior_sum_spend(tmp_path):
    path = str(tmp_path / "j.db")
    pk = "fixed-period"
    via = lambda a: a["amount"]  # noqa: E731

    store1 = EnvelopeStore.from_path(path)
    p1 = Policy()
    p1.add_cap(DurableCap.sum(tool="charge", via=via, max=100.0, store=store1, period=lambda: pk))
    _run_txn(p1, [70.0])
    store1.close()

    store2 = EnvelopeStore.from_path(path)
    assert store2.total("DurableCap.sum(tool='charge', max=100.0)", pk) == 70.0
    p2 = Policy()
    p2.add_cap(DurableCap.sum(tool="charge", via=via, max=100.0, store=store2, period=lambda: pk))
    _run_txn(p2, [30.0])  # total 100 — at cap
    with pytest.raises(PolicyViolation):
        _run_txn(p2, [0.01])  # over
    store2.close()


# --- period rollover --------------------------------------------------------


def test_day_period_rolls_over(tmp_path):
    from datetime import datetime, timezone

    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    d1 = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    d2 = datetime(2026, 5, 22, 0, 1, tzinfo=timezone.utc)
    assert day_period(d1) == "2026-05-21"
    assert day_period(d2) == "2026-05-22"
    # Spend in day 1; day 2 starts fresh.
    store.add("cap", day_period(d1), 50.0)
    assert store.total("cap", day_period(d2)) == 0.0


def test_all_time_period_is_constant():
    assert all_time_period() == "all-time"


def test_pending_increments_skips_zero_and_non_matching(tmp_path):
    store = EnvelopeStore.from_path(str(tmp_path / "j.db"))
    cap = DurableCap.count(tool="charge", max=10, store=store, period=lambda: "p")
    # A journal with no matching effects yields no increments.
    other = Effect(
        txn_id="t", index=0, tool="lookup", args={}, resource="sql", reversible=True
    )
    assert pending_increments([cap], [other]) == []
    # Two matching effects → one increment of amount 2.
    journal = [_charge(1.0, index=0), _charge(1.0, index=1)]
    incs = pending_increments([cap], journal)
    assert len(incs) == 1
    assert incs[0].amount == 2.0
    assert incs[0].cap_name == cap.name
