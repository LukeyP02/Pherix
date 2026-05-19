"""Slice 1 dogfood — a fake multi-step agent against a real SQLite database.

Run:  python examples/slice1_demo.py

Shows the reversible path end-to-end: writes journal live, one mid-sequence
rollback wipes them, a second transaction commits cleanly, and the audit
journal tells the whole story. The "agent" is a plain function calling tools in
sequence — no model, no API key — and it is never transaction-aware.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the example runnable as `python examples/slice1_demo.py` without an
# editable install — put the repo root on the path before importing pherix.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import AuditJournal, SQLiteAdapter, agent_txn, tool


@tool(resource="sql")
def insert_user(conn, name, role):
    conn.execute("INSERT INTO users (name, role) VALUES (?, ?)", (name, role))
    return name


def fake_agent_onboard(team):
    """A plain agent step: call the tool per teammate. No txn awareness."""
    for name, role in team:
        insert_user(name=name, role=role)


def main() -> None:
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, role TEXT)"
    )
    audit = AuditJournal.in_memory()
    adapters = {"sql": SQLiteAdapter(conn)}

    def show(label: str) -> None:
        rows = conn.execute("SELECT name, role FROM users ORDER BY id").fetchall()
        print(f"  DB {label}: {rows}")

    print("Transaction 1 — agent onboards a team, hits a snag, rolls back")
    with agent_txn(adapters, audit=audit) as txn:
        fake_agent_onboard([("ada", "engineer"), ("grace", "enginer")])
        show("mid-transaction (live, journalled)")
        print("  ! agent caught a typo'd role — rolling back the whole step")
        txn.rollback()
    t1 = txn.txn_id
    show("after rollback")
    print()

    print("Transaction 2 — agent retries cleanly and commits")
    with agent_txn(adapters, audit=audit) as txn:
        fake_agent_onboard([("ada", "engineer"), ("grace", "scientist")])
        show("mid-transaction (live, journalled)")
    t2 = txn.txn_id
    show("after commit")
    print()

    print("Audit journal — the whole story:")
    for tid in (t1, t2):
        record = audit.get_transaction(tid)
        print(f"  {tid}  state={record['state']}")
        for effect in audit.get_effects(tid):
            print(
                f"    [{effect['idx']}] {effect['tool']}"
                f"({effect['args']}) -> {effect['status']}"
            )

    conn.close()
    os.remove(db_path)


if __name__ == "__main__":
    main()
