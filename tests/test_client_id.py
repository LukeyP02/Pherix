"""Slice 8 (Stream B): the ``client_id`` audit column — additive provenance.

A gateway front-end (Slice 8) serves many distinct MCP clients through one
core. ``client_id`` is the third instance of the same additive audit pattern
as ``replayed_from`` (Slice 5) and ``dry_run`` (Slice 7): a nullable column on
``transactions``, threaded as a keyword-only param from the entry points down
to :meth:`AuditJournal.record_transaction`, defaulting NULL for library
callers who never supply one.

Tools are defined *inside* each test: the autouse ``_clean_tool_registry``
fixture clears the process-global registry around every test, so a
module-level ``@tool`` would be wiped before the test body runs.
"""

from __future__ import annotations

import sqlite3

import pytest

# Import from core modules directly rather than the top-level ``pherix``
# package: another Slice 8 stream owns ``frontends/library.py`` and may have
# it mid-edit (the gateway re-export), which would otherwise break collection
# of this stream's tests. The public surface still re-exports all of these.
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.dry_run import dry_run
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool


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


def test_library_caller_writes_null_client_id(conn: sqlite3.Connection) -> None:
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as ctx:
        insert_widget(name="a")
        txn_id = ctx.txn_id
    row = audit.get_transaction(txn_id)
    assert row is not None
    assert row["client_id"] is None


def test_agent_txn_round_trips_client_id(conn: sqlite3.Connection) -> None:
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, client_id="aider"
    ) as ctx:
        insert_widget(name="a")
        txn_id = ctx.txn_id
    assert audit.get_transaction(txn_id)["client_id"] == "aider"


def test_dry_run_round_trips_client_id(conn: sqlite3.Connection) -> None:
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with dry_run(
        {"sql": SQLiteAdapter(conn)}, audit=audit, client_id="claude-desktop"
    ) as ctx:
        insert_widget(name="a")
        txn_id = ctx.txn_id
    row = audit.get_transaction(txn_id)
    assert row["client_id"] == "claude-desktop"
    # Dry-run still flags itself as a dry-run alongside the client_id.
    assert row["dry_run"] == 1


def test_record_transaction_default_is_null() -> None:
    """``record_transaction`` with no ``client_id`` writes NULL directly."""
    from pherix.core.transaction import Transaction

    audit = AuditJournal.in_memory()
    txn = Transaction()
    audit.record_transaction(txn)
    assert audit.get_transaction(txn.txn_id)["client_id"] is None
