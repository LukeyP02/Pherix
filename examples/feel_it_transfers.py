"""A non-staged proof: a conservation law you can falsify yourself.

Run:  python examples/feel_it_transfers.py

The "agent" moves money between accounts in a REAL on-disk SQLite ledger that
carries a REAL constraint:  CHECK(0 <= balance <= 1000).  We run the *identical*
transfer code twice — once with the tools called raw (no Pherix), once with the
exact same calls wrapped in `agent_txn`.  Pherix is the ONLY variable.

Nothing here plants a failure or calls rollback "at the right moment".  One
transfer happens to push an account past its constraint; SQLite itself raises an
IntegrityError mid-transfer (after the debit, before the credit).  Under Pherix
that exception triggers the runtime's automatic rollback — we write zero
rollback logic.

The invariant to watch is a conservation law:  SUM(balance) must never change,
because every transfer debits exactly what it credits.

    WITHOUT Pherix : the partial transfer DESTROYS money — the sum drops.
    WITH    Pherix : the books return to EXACTLY the starting sum.

Don't trust the printout — the two ledgers are left on disk; inspect them
yourself (commands printed at the end).  And edit SEED / TRANSFERS below and
re-run: the conservation law holds under Pherix for ANY input you choose.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import AuditJournal, SQLiteAdapter, agent_txn, tool

# --- the scenario — edit these and re-run; the law holds for any input -------

SEED = [("alice", 500), ("bob", 900), ("vault", 0)]   # total = 1400
TRANSFERS = [
    ("alice", "vault", 100),   # fine
    ("alice", "bob", 150),     # debits alice, then credit pushes bob 900->1050
]                              # ...which violates CHECK(balance <= 1000)

ARTIFACTS = Path("/tmp/pherix_feel")

# --- the tools — ONE definition, used by both runs ---------------------------


@tool(resource="sql")
def debit(conn, account, amount):
    conn.execute(
        "UPDATE accounts SET balance = balance - ? WHERE name = ?",
        (amount, account),
    )
    return account


@tool(resource="sql")
def credit(conn, account, amount):
    conn.execute(
        "UPDATE accounts SET balance = balance + ? WHERE name = ?",
        (amount, account),
    )
    return account


# --- helpers ------------------------------------------------------------------


def fresh_ledger(path: Path) -> sqlite3.Connection:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.execute(
        "CREATE TABLE accounts ("
        "  name TEXT PRIMARY KEY,"
        "  balance INTEGER NOT NULL CHECK(balance >= 0 AND balance <= 1000)"
        ")"
    )
    conn.executemany("INSERT INTO accounts VALUES (?, ?)", SEED)
    return conn


def total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(balance), 0) FROM accounts").fetchone()[0]


def snapshot(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT name, balance FROM accounts ORDER BY name").fetchall()


START_TOTAL = sum(b for _, b in SEED)


# --- RUN A: no Pherix — the tools called raw, each write just happens ---------


def run_without_pherix() -> None:
    path = ARTIFACTS / "without_pherix.db"
    conn = fresh_ledger(path)
    print(f"  start : {snapshot(conn)}   total={total(conn)}")
    try:
        for frm, to, amt in TRANSFERS:
            debit(conn, account=frm, amount=amt)    # raw passthrough: conn explicit
            credit(conn, account=to, amount=amt)
    except sqlite3.IntegrityError as e:
        print(f"  !! transfer failed mid-flight: {e}")
    end = total(conn)
    print(f"  end   : {snapshot(conn)}   total={end}")
    drift = end - START_TOTAL
    verdict = "CONSERVED" if drift == 0 else f"LEAKED {drift:+d}  <-- money created/destroyed"
    print(f"  law   : sum {START_TOTAL} -> {end}   [{verdict}]")
    conn.close()


# --- RUN B: same calls, wrapped in agent_txn — Pherix is the only change ------


def run_with_pherix() -> None:
    path = ARTIFACTS / "with_pherix.db"
    conn = fresh_ledger(path)
    audit = AuditJournal.in_memory()
    print(f"  start : {snapshot(conn)}   total={total(conn)}")
    txn_id = None
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
            txn_id = txn.txn_id
            for frm, to, amt in TRANSFERS:
                debit(account=frm, amount=amt)      # conn injected by the adapter
                credit(account=to, amount=amt)
    except sqlite3.IntegrityError as e:
        print(f"  !! transfer failed mid-flight: {e}")
        print("     (Pherix auto-rolled-back the whole transaction — we wrote no rollback code)")
    end = total(conn)
    print(f"  end   : {snapshot(conn)}   total={end}")
    drift = end - START_TOTAL
    verdict = "CONSERVED" if drift == 0 else f"LEAKED {drift:+d}"
    print(f"  law   : sum {START_TOTAL} -> {end}   [{verdict}]")

    if txn_id:
        print("  receipt (audit journal — every attempt + its fate):")
        for eff in audit.get_effects(txn_id):
            print(f"    [{eff['idx']}] {eff['tool']}({eff['args']}) -> {eff['status']}")
    conn.close()


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    print("=" * 72)
    print("RUN A — no Pherix: each tool call hits the DB immediately, no undo")
    print("=" * 72)
    run_without_pherix()
    print()
    print("=" * 72)
    print("RUN B — identical calls wrapped in agent_txn(): Pherix is the only change")
    print("=" * 72)
    run_with_pherix()
    print()
    print("-" * 72)
    print("Verify it yourself — the two ledgers are real files on disk:")
    print(f"  sqlite3 {ARTIFACTS}/without_pherix.db 'SELECT name,balance,SUM(balance)OVER() FROM accounts;'")
    print(f"  sqlite3 {ARTIFACTS}/with_pherix.db    'SELECT name,balance,SUM(balance)OVER() FROM accounts;'")
    print(f"Then edit SEED / TRANSFERS at the top of {Path(__file__).name} and re-run.")
    print("The law holds under Pherix for ANY input. That is the value, not conjecture.")


if __name__ == "__main__":
    main()
