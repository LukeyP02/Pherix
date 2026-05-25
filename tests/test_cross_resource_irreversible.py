"""Slice 3 acceptance — SQL reversible + HTTP irreversible in one transaction.

The CLAUDE.md bar:
  - happy-path commits both
  - rollback before commit unwinds DB via savepoint and never fires HTTP
  - partial-failure-mid-commit unwinds via compensator for HTTP and
    snapshot-restore for SQL
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

# Trust pillar: blast radius — the mixed-fold unwind contains a partial failure
# across both the reversible and irreversible lanes.
pytestmark = pytest.mark.blast_radius


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, status TEXT)"
    )
    yield c
    c.close()


@pytest.fixture
def adapters(conn):
    return {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}


def _orders(conn):
    return [
        (r[1], r[2])
        for r in conn.execute("SELECT * FROM orders ORDER BY id")
    ]


@pytest.fixture
def book_order_charge_email():
    fired_http: list[tuple[str, dict]] = []

    @tool(resource="sql")
    def book_order(c, customer):
        c.execute(
            "INSERT INTO orders (customer, status) VALUES (?, ?)",
            (customer, "booked"),
        )
        return customer

    @tool(resource="http", reversible=False, injects_handle=False)
    def refund_charge(customer, amount):
        fired_http.append(("refund_charge", {"customer": customer, "amount": amount}))

    @tool(
        resource="http", reversible=False, injects_handle=False,
        compensator="refund_charge",
    )
    def charge_card(customer, amount):
        fired_http.append(("charge_card", {"customer": customer, "amount": amount}))

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, body):
        fired_http.append(("send_email", {"to": to, "body": body}))

    return book_order, charge_card, refund_charge, send_email, fired_http


# --- happy path ---


def test_happy_path_commits_sql_and_fires_http(
    conn, adapters, book_order_charge_email
):
    book_order, charge_card, _, send_email, fired = book_order_charge_email
    with agent_txn(adapters) as txn:
        book_order(customer="alice")
        charge_card(customer="alice", amount=100)
        r3 = send_email(to="alice@example.com", body="thanks!")
        txn.approve_irreversible(r3.effect_id)

    assert _orders(conn) == [("alice", "booked")]
    assert fired == [
        ("charge_card", {"customer": "alice", "amount": 100}),
        ("send_email", {"to": "alice@example.com", "body": "thanks!"}),
    ]
    assert txn.txn.state is TxnState.COMMITTED


# --- rollback before commit ---


def test_rollback_before_commit_unwinds_sql_and_never_fires_http(
    conn, adapters, book_order_charge_email
):
    book_order, charge_card, _, send_email, fired = book_order_charge_email
    with agent_txn(adapters) as txn:
        book_order(customer="alice")
        charge_card(customer="alice", amount=100)
        send_email(to="alice@example.com", body="thanks!")
        txn.rollback()

    assert _orders(conn) == []  # SQL rolled back via savepoint
    assert fired == []           # HTTP never fired
    assert txn.txn.state is TxnState.ROLLED_BACK


# --- partial-failure mid-commit ---


def test_partial_failure_mid_commit_compensates_http_and_restores_sql(
    conn, book_order_charge_email
):
    """The acceptance bar word-for-word: 'partial-failure-mid-commit unwinds
    via compensator for HTTP and snapshot-restore for SQL'."""
    book_order, charge_card, _, _, fired = book_order_charge_email

    @tool(resource="http", reversible=False, injects_handle=False)
    def ship_package(customer):
        raise RuntimeError("warehouse offline")

    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    with pytest.raises(RuntimeError, match="warehouse offline"):
        with agent_txn(adapters) as txn:
            book_order(customer="alice")
            charge_card(customer="alice", amount=100)
            r3 = ship_package(customer="alice")
            txn.approve_irreversible(r3.effect_id)

    # SQL: savepoint-restored (no order persisted).
    assert _orders(conn) == []
    # HTTP charge: compensated via its refund.
    assert ("refund_charge", {"customer": "alice", "amount": 100}) in fired
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_partial_failure_with_missing_compensator_leaves_sql_unwound_anyway(
    conn, book_order_charge_email
):
    """STUCK should still roll back the SQL side: the operator's recovery
    target is the irreversible-only journal, not a half-applied DB."""
    book_order, _, _, _, _ = book_order_charge_email

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_no_comp(x):
        return None  # fires successfully, no compensator

    @tool(resource="http", reversible=False, injects_handle=False)
    def step_failing(x):
        raise RuntimeError("boom")

    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn(adapters) as txn:
            book_order(customer="alice")
            r2 = step_no_comp(x=1)
            r3 = step_failing(x=2)
            txn.approve_irreversible(r2.effect_id)
            txn.approve_irreversible(r3.effect_id)

    assert _orders(conn) == []
    assert txn.txn.state is TxnState.STUCK


def test_audit_records_full_cross_resource_story(
    conn, adapters, book_order_charge_email
):
    book_order, charge_card, _, send_email, _ = book_order_charge_email
    audit = AuditJournal.in_memory()
    with agent_txn(adapters, audit=audit) as txn:
        book_order(customer="alice")
        charge_card(customer="alice", amount=100)
        r3 = send_email(to="alice@example.com", body="thanks!")
        txn.approve_irreversible(r3.effect_id)

    effects = audit.get_effects(txn.txn_id)
    # Every effect ends up APPLIED; the journal carries the whole story.
    assert [(e["idx"], e["resource"], e["status"]) for e in effects] == [
        (0, "sql", "APPLIED"),
        (1, "http", "APPLIED"),
        (2, "http", "APPLIED"),
    ]
    assert audit.get_transaction(txn.txn_id)["state"] == "COMMITTED"
