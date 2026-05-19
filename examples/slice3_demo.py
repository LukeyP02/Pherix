"""Slice 3 dogfood — staged irreversibles, the gate, and partial-commit unwind.

Run:  python examples/slice3_demo.py

A fake agent books an order (reversible SQL write), charges a card (staged
HTTP, compensator-backed), and sends a receipt email (staged HTTP, no
compensator → needs approval). Three scenarios:

  1. Happy path. The approver approves the email. commit() fires the staged
     irreversibles in journal order; SQL persists; txn ends COMMITTED.

  2. Gate block. The approver refuses to approve the email. commit() blocks
     at the gate, the reversible SQL write is unwound via savepoint, and no
     HTTP call ever fires. Txn ends ROLLED_BACK.

  3. Partial-commit unwind. The shipping API is down; ship_package raises
     mid-commit. Pherix walks the journal backward in a *mixed fold* —
     ``compensator(effect)`` for the already-fired charge, snapshot-restore
     for the SQL booking. Txn ends ROLLED_BACK with the world un-touched.

The "HTTP service" is a deterministic in-memory fake — no network call ever
escapes the process; the audit journal still shows the entire story.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the example runnable as `python examples/slice3_demo.py` without an
# editable install — put the repo root on the path before importing pherix.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (
    AuditJournal,
    GateBlocked,
    HTTPAdapter,
    SQLiteAdapter,
    agent_txn,
    tool,
)


# --- the "external HTTP service" — a deterministic in-memory fake ---------
#
# A real Pherix user would call requests / httpx / stripe-sdk here. The
# demo keeps it offline so the slice's acceptance is a script that runs
# anywhere, with no API keys, no network, no flakiness.


class FakeStripe:
    """Tracks charges and refunds the demo issues so we can show the journal."""

    def __init__(self) -> None:
        self.charges: list[dict] = []
        self.refunds: list[dict] = []
        self.emails: list[dict] = []
        self.shipped: list[dict] = []
        self.shipping_works = True

    def charge(self, customer: str, amount: int) -> dict:
        self.charges.append({"customer": customer, "amount": amount})
        return {"charge_id": f"ch_{len(self.charges)}", "customer": customer}

    def refund(self, customer: str, amount: int) -> None:
        self.refunds.append({"customer": customer, "amount": amount})

    def email(self, to: str, body: str) -> None:
        self.emails.append({"to": to, "body": body})

    def ship(self, customer: str) -> None:
        if not self.shipping_works:
            raise RuntimeError("warehouse offline")
        self.shipped.append({"customer": customer})


STRIPE = FakeStripe()


# --- the agent's @tool surface --------------------------------------------


@tool(resource="sql")
def book_order(conn, customer: str):
    conn.execute(
        "INSERT INTO orders (customer, status) VALUES (?, ?)",
        (customer, "booked"),
    )


@tool(resource="http", reversible=False, injects_handle=False)
def refund_charge(customer: str, amount: int):
    """The compensator for charge_card. Same args; idempotency by customer
    + amount is the developer's responsibility (D2)."""
    STRIPE.refund(customer, amount)


@tool(
    resource="http", reversible=False, injects_handle=False,
    compensator="refund_charge",
)
def charge_card(customer: str, amount: int):
    return STRIPE.charge(customer, amount)


@tool(resource="http", reversible=False, injects_handle=False)
def send_email(to: str, body: str):
    """No compensator — an email cannot be un-sent. Needs explicit approval
    via approve_irreversible(effect_id) before commit."""
    STRIPE.email(to, body)


@tool(resource="http", reversible=False, injects_handle=False)
def ship_package(customer: str):
    """No compensator. Used in scenario 3 to simulate a third-party failure
    mid-commit."""
    STRIPE.ship(customer)


# --- a "human-in-the-loop approver" — same role for any out-of-band actor -


def approver_approves(txn, effect_id: str) -> None:
    print(f"  approver: APPROVE  effect_id={effect_id}")
    txn.approve_irreversible(effect_id)


def approver_refuses(txn, effect_id: str) -> None:
    print(f"  approver: REFUSE   effect_id={effect_id}")


# --- the demo --------------------------------------------------------------


def show_db_and_stripe(conn, label: str) -> None:
    rows = conn.execute(
        "SELECT customer, status FROM orders ORDER BY id"
    ).fetchall()
    print(f"  {label}")
    print(f"    DB orders : {rows}")
    print(f"    charges   : {STRIPE.charges}")
    print(f"    refunds   : {STRIPE.refunds}")
    print(f"    emails    : {STRIPE.emails}")
    print(f"    shipped   : {STRIPE.shipped}")


def scenario_happy(conn, audit) -> str:
    print("=" * 72)
    print("Scenario 1 — happy path: book, charge, send email; approver OK")
    print("=" * 72)
    show_db_and_stripe(conn, "before")
    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    with agent_txn(adapters, audit=audit) as txn:
        book_order(customer="alice")
        charge_card(customer="alice", amount=4200)
        receipt = send_email(to="alice@example.com", body="receipt")
        print(f"  agent staged 3 effects; sentinel for email = {receipt}")
        # Out-of-band approval: a human / policy / guardrail decides whether
        # the un-compensable effect should fire. Pherix records the verdict.
        approver_approves(txn, receipt.effect_id)
    show_db_and_stripe(conn, "after commit")
    print(f"  txn state = {txn.txn.state.name}")
    print()
    return txn.txn_id


def scenario_gate_block(conn, audit) -> str:
    print("=" * 72)
    print("Scenario 2 — gate block: approver refuses the email")
    print("=" * 72)
    show_db_and_stripe(conn, "before")
    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    try:
        with agent_txn(adapters, audit=audit) as txn:
            book_order(customer="bob")
            charge_card(customer="bob", amount=1500)
            receipt = send_email(to="bob@example.com", body="receipt")
            approver_refuses(txn, receipt.effect_id)
            # No call to approve_irreversible() → commit will block at the gate.
    except GateBlocked as exc:
        print(f"  commit blocked → {exc}")
    show_db_and_stripe(conn, "after gate block")
    print(f"  txn state = {txn.txn.state.name}")
    print("  ! charge & email never fired; SQL booking restored via savepoint")
    print()
    return txn.txn_id


def scenario_partial_unwind(conn, audit) -> str:
    print("=" * 72)
    print("Scenario 3 — partial commit: shipping fails mid-fire")
    print("=" * 72)
    show_db_and_stripe(conn, "before")
    STRIPE.shipping_works = False
    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    try:
        with agent_txn(adapters, audit=audit) as txn:
            book_order(customer="carol")
            charge_card(customer="carol", amount=9900)  # has compensator
            ship_token = ship_package(customer="carol")  # no compensator
            approver_approves(txn, ship_token.effect_id)
    except RuntimeError as exc:
        print(f"  third-party blew up → {exc}")
    show_db_and_stripe(conn, "after partial unwind")
    print(f"  txn state = {txn.txn.state.name}")
    print("  ! charge_card was compensated via refund_charge in reverse order")
    print("  ! SQL booking restored via savepoint")
    print()
    STRIPE.shipping_works = True
    return txn.txn_id


def show_journal(audit, t1: str, t2: str, t3: str) -> None:
    print("=" * 72)
    print("Audit journal — the whole story, all three transactions")
    print("=" * 72)
    for tid, label in [
        (t1, "happy"),
        (t2, "gate-blocked"),
        (t3, "partial-unwind"),
    ]:
        record = audit.get_transaction(tid)
        print(f"  {tid}  [{label:14s}]  state={record['state']}")
        for effect in audit.get_effects(tid):
            print(
                f"    [{effect['idx']}] {effect['resource']:>4}  "
                f"{effect['tool']}({effect['args']}) -> {effect['status']}"
            )
        print()


def main() -> None:
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, status TEXT)"
    )

    audit = AuditJournal.in_memory()

    t1 = scenario_happy(conn, audit)
    t2 = scenario_gate_block(conn, audit)
    t3 = scenario_partial_unwind(conn, audit)

    show_journal(audit, t1, t2, t3)

    conn.close()
    os.remove(db_path)
    # Reset the in-memory fake so re-runs of the demo are idempotent.
    STRIPE.charges.clear()
    STRIPE.refunds.clear()
    STRIPE.emails.clear()
    STRIPE.shipped.clear()


if __name__ == "__main__":
    main()
