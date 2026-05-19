"""Slice 4 dogfood — two fake agents racing on a shared resource.

Run:  python examples/slice4_demo.py

Two agents (A and B) both want to credit the same bank-account row in a
shared SQLite ledger. They open their own ``agent_txn`` blocks against
their own connection to the file. Without isolation, the classic
lost-update goes through (A reads balance=0, B writes balance=10 and
commits, A writes balance=A_read+5=5 and commits — B's update is lost).

Slice 4 makes that scenario impossible. The agents declare their reads
and writes via ``execute_isolated(...)``; Pherix records the read versions
into each effect's journal; at commit-time the diff folds the journal
against current adapter state and the resolution policy fires.

Three scenarios show the same conflict producing three different,
serializability-preserving outcomes:

  1. ``Abort()``     — first-committer wins; the loser raises
                       ``IsolationConflict``. Default.
  2. ``Retry(2)``    — Pherix re-runs the loser's body against the
                       post-winner state via ``run_txn(fn, ...)``.
  3. ``Serialize()`` — the loser's commit blocks (in-process) until any
                       concurrent in-flight writer has closed; with no
                       writer in flight the wait is a no-op and the
                       diff proceeds. (Cross-process Serialize degrades
                       to Abort — D5 single-host scope.)

Maths framing: ``isolation`` is a callable ``f: Conflict -> Action`` the
operator picks per transaction. Slice 4 ships three such ``f``s.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# Run as a script without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (  # noqa: E402
    Abort,
    AuditJournal,
    IsolationConflict,
    Retry,
    SQLiteAdapter,
    Serialize,
    agent_txn,
    run_txn,
    tool,
)
from pherix.core.adapters.sql import execute_isolated  # noqa: E402


# --- the agent's @tool surface ---------------------------------------------


@tool(resource="sql")
def read_balance(conn, account):
    cur = execute_isolated(
        conn,
        "SELECT val FROM accounts WHERE name = ?",
        (account,),
        reads=[("accounts", account)],
    )
    row = cur.fetchone()
    return row[0] if row else 0


@tool(resource="sql")
def credit(conn, account, amount):
    execute_isolated(
        conn,
        "UPDATE accounts SET val = val + ? WHERE name = ?",
        (amount, account),
        writes=[("accounts", account)],
    )


# --- helpers --------------------------------------------------------------


def setup_db(path: str) -> None:
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, val INTEGER)")
    c.execute("INSERT INTO accounts VALUES ('alice', 0)")
    c.close()


def reset_db(path: str) -> None:
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("UPDATE accounts SET val = 0 WHERE name = 'alice'")
    # The version table is created by SQLiteAdapter — may not exist yet
    # on the first scenario. Idempotent table-create then reset.
    c.execute(
        "CREATE TABLE IF NOT EXISTS _pherix_versions "
        "(resource TEXT, key_json TEXT, version INTEGER DEFAULT 0, "
        "PRIMARY KEY (resource, key_json))"
    )
    c.execute(
        "UPDATE _pherix_versions SET version = 0 "
        "WHERE resource = 'sql' AND key_json = ?",
        ('["accounts", "alice"]',),
    )
    c.close()


def show_balance(path: str, label: str) -> None:
    c = sqlite3.connect(path)
    val = c.execute("SELECT val FROM accounts WHERE name='alice'").fetchone()[0]
    c.close()
    print(f"    {label:24s} balance(alice) = {val}")


# --- scenarios -------------------------------------------------------------


def scenario_abort(db_path: str, audit: AuditJournal) -> None:
    print("=" * 72)
    print("Scenario 1 — Abort: A reads 0, B credits 10 & commits, A's diff fires.")
    print("=" * 72)
    reset_db(db_path)

    conn_a = sqlite3.connect(db_path, isolation_level=None)
    conn_b = sqlite3.connect(db_path, isolation_level=None)
    ad_a = SQLiteAdapter(conn_a)
    ad_b = SQLiteAdapter(conn_b)

    show_balance(db_path, "before")
    # A is the "stale reader" — it observes the balance, then would
    # take some external action based on that observation. While A is
    # mid-decision, B commits a write that invalidates A's reading. At
    # A's commit-time diff the read version has moved → IsolationConflict.
    # (We keep A read-only here so the conflict isolates to the read
    # path; the lost-update where A also writes is the same shape, just
    # with extra SQLite lock contention that SQLite already serialises.)
    try:
        with agent_txn({"sql": ad_a}, isolation=Abort(), audit=audit) as ctx_a:
            bal = read_balance(account="alice")
            print(f"    A read balance = {bal}  (snapshot fixed for A)")
            print("    [agent B opens its own txn and credits 10]")
            with agent_txn({"sql": ad_b}, audit=audit) as ctx_b:
                credit(account="alice", amount=10)
            print(f"    B committed (state={ctx_b.txn.state.name})")
            print("    A reaches end-of-block; auto-commit triggers the diff...")
    except IsolationConflict as exc:
        keys = [(c.key, c.version_at_read, c.version_now) for c in exc.conflicts]
        print(f"    A commit -> IsolationConflict: {keys}")
        print(f"    A state  = {ctx_a.txn.state.name}")

    show_balance(db_path, "after")
    print(
        "    ! A's downstream action would have used a stale read; the diff\n"
        "      caught it and A unwound cleanly. B's credit stands.\n"
    )

    conn_a.close()
    conn_b.close()


def scenario_retry(db_path: str, audit: AuditJournal) -> None:
    print("=" * 72)
    print("Scenario 2 — Retry(2): Pherix replays A's body against the post-B world.")
    print("=" * 72)
    reset_db(db_path)

    conn_a = sqlite3.connect(db_path, isolation_level=None)
    conn_b = sqlite3.connect(db_path, isolation_level=None)
    ad_a = SQLiteAdapter(conn_a)
    ad_b = SQLiteAdapter(conn_b)

    show_balance(db_path, "before")
    attempts = {"n": 0}
    reads: list[int] = []

    def body(ctx):
        attempts["n"] += 1
        bal = read_balance(account="alice")
        reads.append(bal)
        print(f"    A attempt #{attempts['n']}: read balance = {bal}")
        if attempts["n"] == 1:
            # On attempt 1, B steps in between A's read and A's commit.
            # On attempt 2 (the retry), there is no concurrent B — A
            # sees the post-B world, the diff is clean, A commits.
            print("    [agent B opens its own txn and credits 10]")
            with agent_txn({"sql": ad_b}, audit=audit) as ctx_b:
                credit(account="alice", amount=10)
            print(f"    B committed (state={ctx_b.txn.state.name})")

    run_txn(body, {"sql": ad_a}, isolation=Retry(max_attempts=3), audit=audit)
    print(f"    A succeeded after {attempts['n']} attempt(s); reads = {reads}")

    show_balance(db_path, "after")
    print(
        "    ! Pherix replayed A's body — the second attempt observed B's\n"
        "      committed write. Caller logic stays simple: write a function,\n"
        "      hand it to run_txn, let isolation drive correctness.\n"
    )

    conn_a.close()
    conn_b.close()


def scenario_serialize(db_path: str, audit: AuditJournal) -> None:
    print("=" * 72)
    print("Scenario 3 — Serialize: A's commit waits for any in-flight writer to close.")
    print("=" * 72)
    reset_db(db_path)

    # Single-host single-process. In-flight writer B runs on a separate
    # thread; Serialize finds B in the JournalRegistry and waits on B's
    # close-event before running the diff. SQLite connections are
    # thread-affine, so B opens its own connection inside its worker.
    conn_a = sqlite3.connect(db_path, isolation_level=None)
    ad_a = SQLiteAdapter(conn_a)

    show_balance(db_path, "before")

    b_done = threading.Event()
    b_can_finish = threading.Event()

    def b_worker():
        conn_b = sqlite3.connect(db_path, isolation_level=None)
        ad_b = SQLiteAdapter(conn_b)
        # Audit journals are SQLite connections too — thread-affine.
        # Give B its own so the worker thread never touches A's audit.
        b_audit = AuditJournal.in_memory()
        try:
            with agent_txn({"sql": ad_b}, audit=b_audit) as _:
                credit(account="alice", amount=10)
                b_can_finish.wait(timeout=10)
        finally:
            b_audit.close()
            conn_b.close()
            b_done.set()

    t = threading.Thread(target=b_worker, daemon=True)
    t.start()
    time.sleep(0.05)  # let B reach the wait

    print("    [B is in-flight, holding its write uncommitted]")

    # Release B from a side-thread once A's wait has started.
    def release_b():
        time.sleep(0.1)  # let A start waiting
        b_can_finish.set()

    threading.Thread(target=release_b, daemon=True).start()

    try:
        with agent_txn(
            {"sql": ad_a},
            isolation=Serialize(timeout_seconds=5.0),
            audit=audit,
        ) as ctx_a:
            bal = read_balance(account="alice")
            print(f"    A read balance = {bal}  (B's write not yet visible)")
            # A's auto-commit triggers the Serialize wait — A blocks
            # until B closes. After B closes the diff runs; A read v0
            # but B committed v1, so Serialize degrades to Abort and
            # IsolationConflict propagates.
    except IsolationConflict as exc:
        keys = [(c.key, c.version_at_read, c.version_now) for c in exc.conflicts]
        print(f"    A post-wait diff -> IsolationConflict: {keys}")

    b_done.wait(timeout=5)

    show_balance(db_path, "after")
    print(
        "    ! Serialize gave A a serializable ordering: A waited for B's\n"
        "      decision before checking; B's commit then triggered the\n"
        "      conflict on A's read-set. (Cross-process Serialize would\n"
        "      degrade to Abort — see D5.)\n"
    )

    conn_a.close()


# --- the demo -------------------------------------------------------------


def main() -> None:
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    setup_db(db_path)
    audit = AuditJournal.in_memory()

    try:
        scenario_abort(db_path, audit)
        scenario_retry(db_path, audit)

        scenario_serialize(db_path, audit)
    finally:
        os.remove(db_path)


if __name__ == "__main__":
    main()
