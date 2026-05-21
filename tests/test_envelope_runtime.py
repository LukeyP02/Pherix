"""#10 end-to-end: the runtime's successful-commit flush consumes durable budget.

A's :mod:`tests.test_envelope` pins the store/cap layer and *simulates* the
flush with ``pending_increments`` + ``flush_increments``. This module pins the
orchestrator's runtime wiring: a real ``agent_txn`` commit must fold the
journal into the durable total, and a rolled-back / denied run must consume
nothing. The flush hook lives in ``TxnContext.commit`` (only the
successful-commit path).
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.envelope import DurableCap, EnvelopeStore
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.tools import tool


_FIXED_PERIOD = lambda: "fixed-period"  # noqa: E731 — pin the bucket for tests
_CAP_NAME = "DurableCap.sum(tool='charge', max=100.0)"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE charges (id INTEGER PRIMARY KEY, amount REAL)")
    yield c
    c.close()


@pytest.fixture
def charge():
    # Defined inside a fixture: the autouse registry-clean fixture wipes the
    # process-global REGISTRY between tests, so a module-level @tool would not
    # survive into the test body.
    @tool(resource="sql")
    def charge(conn, amount):
        conn.execute("INSERT INTO charges (id, amount) VALUES (NULL, ?)", (amount,))
        return amount

    return charge


def _policy(store: EnvelopeStore) -> Policy:
    p = Policy.allow_all()
    p.add_cap(
        DurableCap.sum(
            tool="charge",
            via=lambda a: a["amount"],
            max=100.0,
            store=store,
            period=_FIXED_PERIOD,
        )
    )
    return p


def test_committed_runs_share_durable_budget_via_runtime(conn, tmp_path, charge):
    """Two real agent_txn commits fold into one cross-run total; a third that
    would exceed it is denied at stage-time and rolls back."""
    audit = AuditJournal(str(tmp_path / "j.db"))
    store = EnvelopeStore.from_audit(audit)
    policy = _policy(store)

    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit):
        charge(amount=60.0)
    assert store.total(_CAP_NAME, "fixed-period") == 60.0

    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit):
        charge(amount=30.0)
    assert store.total(_CAP_NAME, "fixed-period") == 90.0

    # Third run: baseline 90 + 20 = 110 > 100 -> denied at stage-time; the
    # txn unwinds and the durable total is untouched (no flush on the
    # denied path).
    with pytest.raises(PolicyViolation, match="durable sum cap"):
        with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit):
            charge(amount=20.0)
    assert store.total(_CAP_NAME, "fixed-period") == 90.0


def test_rolled_back_run_consumes_no_durable_budget_via_runtime(conn, tmp_path, charge):
    """A run whose body raises (auto-rollback) must spend nothing — the flush
    is on the commit path only."""
    audit = AuditJournal(str(tmp_path / "j.db"))
    store = EnvelopeStore.from_audit(audit)
    policy = _policy(store)

    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit):
            charge(amount=90.0)
            raise RuntimeError("boom")

    assert store.total(_CAP_NAME, "fixed-period") == 0.0
    # Full budget still available afterwards.
    with agent_txn({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit):
        charge(amount=90.0)
    assert store.total(_CAP_NAME, "fixed-period") == 90.0
