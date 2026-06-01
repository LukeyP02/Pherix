"""Prong #2 (A) — conflicts persisted at the runtime seam.

The commit-time diff (``_run_isolation_check``) used to hand a conflict
straight to the resolution policy, which raised; the journal never saw it.
These pin that the runtime now persists the conflict as a first-class journal
record BEFORE the policy runs — so the record survives the Abort raise — and
that the reader counts it (``stats()["conflict_total"]``) and attaches it to
the per-txn timeline.

Real adapters, one Python process, an explicit in-memory journal so the test
can read the record back. Offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.isolation import Abort, IsolationConflict
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY as TOOL_REGISTRY, tool
from pherix.core.transaction import TxnState
from pherix.inspector.reader import JournalReader


@pytest.fixture(autouse=True)
def _isolate_tool_registry():
    snapshot = dict(TOOL_REGISTRY._tools)
    yield
    TOOL_REGISTRY._tools = snapshot


@pytest.fixture
def shared_db(tmp_path: Path) -> Path:
    db = tmp_path / "shared.db"
    boot = sqlite3.connect(str(db), isolation_level=None)
    boot.execute("PRAGMA journal_mode=WAL")
    boot.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER)")
    boot.execute("INSERT INTO counters VALUES ('x', 0)")
    boot.close()
    return db


def _adapter(db: Path) -> tuple[sqlite3.Connection, SQLiteAdapter]:
    conn = sqlite3.connect(str(db), isolation_level=None)
    return conn, SQLiteAdapter(conn)


def test_conflict_is_persisted_and_counted_under_abort(shared_db: Path, tmp_path: Path):
    """A reads x@0; B writes x and commits (v→1); A's commit diff fires under
    Abort. The conflict must already be in the journal by the time the raise
    propagates — Abort raising must not erase the record.

    Fails against the prior commit: there was no ``conflicts`` table, no
    ``record_conflicts`` call in ``_run_isolation_check``, and no
    ``conflict_total`` on ``stats()``.
    """
    journal_path = str(tmp_path / "journal.db")
    audit = AuditJournal(journal_path)
    conn_a, ad_a = _adapter(shared_db)
    conn_b, ad_b = _adapter(shared_db)
    try:

        @tool(resource="sql")
        def read_x(conn, name):
            cur = execute_isolated(
                conn,
                "SELECT val FROM counters WHERE name = ?",
                (name,),
                reads=[("counters", name)],
            )
            return cur.fetchone()[0]

        @tool(resource="sql")
        def write_x(conn, name, val):
            execute_isolated(
                conn,
                "UPDATE counters SET val = ? WHERE name = ?",
                (val, name),
                writes=[("counters", name)],
            )

        txn_id = {}
        with pytest.raises(IsolationConflict):
            with agent_txn({"sql": ad_a}, audit=audit, isolation=Abort()) as ctx_a:
                txn_id["a"] = ctx_a.txn_id
                assert read_x(name="x") == 0
                with agent_txn({"sql": ad_b}, audit=audit) as _:
                    write_x(name="x", val=99)
                # A's auto-commit fires the diff → conflict → Abort raises.

        # The conflict survived the raise: it is in the journal. The audit
        # layer returns the raw DB rows (key/versions are JSON columns).
        recorded = audit.get_conflicts(txn_id["a"])
        assert len(recorded) == 1
        assert recorded[0]["resource"] == "sql"
        assert json.loads(recorded[0]["key"]) == ["counters", "x"]
        assert json.loads(recorded[0]["version_at_read"]) == 0
        assert json.loads(recorded[0]["version_now"]) == 1
        # A rolled back; the txn is settled ROLLED_BACK.
        assert ctx_a.txn.state is TxnState.ROLLED_BACK
    finally:
        conn_a.close()
        conn_b.close()
        audit.close()

    # And the reader counts it + attaches it to the per-txn timeline.
    with JournalReader(journal_path) as r:
        assert r.stats()["conflict_total"] == 1
        tl = r.get_timeline(txn_id["a"])
        assert len(tl["conflicts"]) == 1
        assert tl["conflicts"][0]["key"] == ["counters", "x"]
        assert tl["conflicts"][0]["version_now"] == 1


def test_no_conflict_records_nothing(shared_db: Path, tmp_path: Path):
    """A clean commit with no concurrent writer records zero conflicts —
    the diff returns early and never reaches ``record_conflicts``."""
    journal_path = str(tmp_path / "journal.db")
    audit = AuditJournal(journal_path)
    conn, adapter = _adapter(shared_db)
    try:

        @tool(resource="sql")
        def read_x(conn, name):
            cur = execute_isolated(
                conn,
                "SELECT val FROM counters WHERE name = ?",
                (name,),
                reads=[("counters", name)],
            )
            return cur.fetchone()[0]

        with agent_txn({"sql": adapter}, audit=audit) as ctx:
            txn_id = ctx.txn_id
            assert read_x(name="x") == 0
        assert ctx.txn.state is TxnState.COMMITTED
        assert audit.get_conflicts(txn_id) == []
    finally:
        conn.close()
        audit.close()

    with JournalReader(journal_path) as r:
        assert r.stats()["conflict_total"] == 0
