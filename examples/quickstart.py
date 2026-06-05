"""Pherix quickstart — watch a rollback and a gate, in ~40 lines, fully offline.

    python examples/quickstart.py

No API key, no model, no network. Two tools: one *reversible* database write and
one *irreversible* "send". Pherix undoes the first on demand and gates the second
at commit — because an email can't be un-sent, so it must not fire until approved.
"""

import sqlite3

from pherix import AuditJournal, GateBlocked, HTTPAdapter, SQLiteAdapter, agent_txn, tool


# A reversible tool: a DB write. The connection is injected as the first arg;
# the agent body that calls add_user(name=...) never sees it.
@tool(resource="sql")
def add_user(conn, name):
    conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    return name


# An irreversible tool: a "send" with no semantic inverse. We record what was
# actually sent so we can prove, below, that a gated send never fired.
sent: list[tuple[str, str]] = []


@tool(resource="http", reversible=False, injects_handle=False)
def send_email(to, body):
    sent.append((to, body))
    return "sent"


def main() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE users (name TEXT)")

    def users() -> list[str]:
        return [r[0] for r in conn.execute("SELECT name FROM users")]

    # --- 1. rollback undoes the reversible write -------------------------------
    print("=== rollback ===")
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=AuditJournal.in_memory()) as txn:
        add_user(name="ada")
        print("  inside txn:      ", users())   # ['ada']
        txn.rollback()
    print("  after rollback:  ", users())       # []  <- nothing persisted

    # --- 2. the gate blocks an un-undoable effect at commit --------------------
    print("\n=== gate (not approved) ===")
    adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
    try:
        with agent_txn(adapters, audit=AuditJournal.in_memory()):
            add_user(name="grace")
            send_email(to="grace@example.com", body="welcome")
            # Leaving the block triggers commit. send_email has no compensator,
            # so commit BLOCKS at the gate — we never approved it.
    except GateBlocked as blocked:
        print("  commit blocked:  ", blocked)
        print("  emails sent:     ", sent)      # []  <- never fired
        print("  users persisted: ", users())   # []  <- the DB write rolled back too

    # --- 3. approve the irreversible, and it goes through ----------------------
    print("\n=== gate (approved) ===")
    with agent_txn(adapters, audit=AuditJournal.in_memory()) as txn:
        add_user(name="grace")
        receipt = send_email(to="grace@example.com", body="welcome")
        txn.approve_irreversible(receipt.effect_id)
    print("  emails sent:     ", sent)          # [('grace@example.com', 'welcome')]
    print("  users persisted: ", users())       # ['grace']


if __name__ == "__main__":
    main()
