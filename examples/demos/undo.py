"""Pherix flagship demo — UNDO: a wrong-but-allowed write, then a real rollback.

    python examples/demos/undo.py

The scenario is the one the whole product is pitched on. An agent does NOT do
anything *malicious* — it runs a plausible, legitimate-looking, policy-allowed
write that happens to be **wrong**: a bulk status update with an over-broad
``WHERE`` clause that touches every row instead of the handful it meant to. The
classic agent failure isn't the obviously-evil command a guardrail catches; it's
the reasonable-looking one that does too much.

Pherix sits at the tool-call layer. The wrong write executes **live** against a
real SQLite database, journalled, behind a real ``SAVEPOINT``. Then a single
``rollback()`` runs ``ROLLBACK TO SAVEPOINT`` and every one of the ~40,000 rows
is restored — exactly, by the backend, not by a guessed inverse. The demo prints
a clean before -> wrong -> after narrative and proves *zero rows lost*.

The mental model (for the maths reader): the journal is an append-only sequence
of effects; ``commit`` is a forward fold and ``rollback`` is a backward fold over
it. This demo runs the body, then folds backward once. The adapter's
``(snapshot, apply, restore)`` triple is the per-effect undo — here
``snapshot`` = ``SAVEPOINT``, ``restore`` = ``ROLLBACK TO SAVEPOINT``. No
knowledge of *what the UPDATE meant* is needed; the database reverts state
exactly.

------------------------------------------------------------------------------
THIS FILE IS THE TEMPLATE for the other per-feature demos. Each demo is the
same five-part skeleton:

  1. TOOLS    — @tool functions: the real side-effecting operations, one per
                resource. The agent body calls these; it is never txn-aware.
  2. SEED     — build a *real* backend with realistic scale (~40k rows here).
  3. SCENARIO — the agent body: a plain function that calls tools in sequence.
                One feature per demo decides what happens after the body runs
                (here: rollback; elsewhere: gate, compensate, replay, ...).
  4. NARRATE  — print before -> action -> after off the REAL resource, plus the
                journal. Numbers come from the database, never hard-coded.
  5. EMIT     — persist the journal to a SQLite file (so the inspector can
                render/animate it) AND dump a clip-source JSON (the alternative
                animate path). Both are derived from the same journal.

Run it, then animate it two ways:

    # live console — polls the journal and animates effects landing/unwinding
    python -m pherix.inspector --db examples/demos/.out/undo.db
    # then open http://127.0.0.1:8765

    # clip-source JSON for the player (printed path, also under .out/)
    examples/demos/.out/undo.clip.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Run as `python examples/demos/undo.py` with no editable install: put the repo
# root on the path before importing pherix. (Repo root is three levels up:
# examples/demos/undo.py -> examples/demos -> examples -> repo root.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from pherix import AuditJournal, SQLiteAdapter, agent_txn, tool  # noqa: E402

# Where this run's evidence lands. Gitignored (examples/ + *.db), regenerated
# every run — these are evidence, not source.
OUT_DIR = Path(__file__).resolve().parent / ".out"
JOURNAL_DB = OUT_DIR / "undo.db"
CLIP_JSON = OUT_DIR / "undo.clip.json"

N_ORDERS = 40_000


# --- 1. TOOLS ---------------------------------------------------------------
#
# The real side-effecting operations. ``conn`` is injected by the SQL adapter at
# apply-time and hidden from the agent's call-site — the body calls
# ``set_status(...)`` with no connection in sight. Every call inside an
# ``agent_txn`` block becomes one journalled Effect, routed to the "sql" adapter.


@tool(resource="sql")
def set_status(conn: sqlite3.Connection, where: str, status: str) -> int:
    """Bulk-update order status. ``where`` is a SQL predicate (no ``WHERE``).

    This is the *plausible-but-wrong* operation: the agent means to void a few
    stale orders, but builds an over-broad predicate (``"1=1"``) that matches
    every row. The tool itself is innocent and policy-allowed — the damage is
    in the argument the agent chose. Parameterised on ``status``; ``where`` is
    a predicate the calling agent controls (the realistic attack surface — a
    wrong filter, not a malicious tool).
    """
    cur = conn.execute(f"UPDATE orders SET status = ? WHERE {where}", (status,))
    return cur.rowcount


# --- 2. SEED ----------------------------------------------------------------


def seed_orders(conn: sqlite3.Connection, n: int) -> None:
    """Build a realistic ``orders`` table: ``n`` rows, a spread of statuses."""
    conn.execute("DROP TABLE IF EXISTS orders")
    conn.execute(
        "CREATE TABLE orders ("
        "  id INTEGER PRIMARY KEY,"
        "  customer_id INTEGER NOT NULL,"
        "  amount_cents INTEGER NOT NULL,"
        "  status TEXT NOT NULL"
        ")"
    )
    statuses = ("paid", "shipped", "refunded", "pending")
    rows = [
        (i, i % 5000, (i * 37) % 50_000 + 100, statuses[i % len(statuses)])
        for i in range(1, n + 1)
    ]
    conn.executemany(
        "INSERT INTO orders (id, customer_id, amount_cents, status) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def status_histogram(conn: sqlite3.Connection) -> dict[str, int]:
    """The fact the narrative reads off the real table — never hard-coded."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY status"
        ).fetchall()
    }


def count_voided(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status = 'void'"
    ).fetchone()[0]


# --- 3. SCENARIO ------------------------------------------------------------


def agent_body() -> int:
    """The agent's plan: 'void the stale orders'. It builds the predicate
    itself — and gets it wrong (``1=1`` matches everything). A plain function
    calling a tool; no model, no API key, never transaction-aware.

    Returns the row count the wrong write reported, for the narrative.
    """
    # What the agent INTENDED: orders pending for a specific dead customer.
    # What it actually built: an over-broad predicate that voids the whole table.
    overbroad_predicate = "1=1"
    return set_status(where=overbroad_predicate, status="void")


# --- 4. NARRATE -------------------------------------------------------------


def _fmt_hist(hist: dict[str, int]) -> str:
    return ", ".join(f"{k}={v:,}" for k, v in hist.items())


def narrate(conn: sqlite3.Connection, journal: AuditJournal, txn_id: str) -> None:
    """Print the journal effect-by-effect: tool, status, the live-then-unwound
    story straight off the persisted journal."""
    record = journal.get_transaction(txn_id)
    print(f"\n  journal  txn={txn_id}  final state = {record['state']}")
    for e in journal.get_effects(txn_id):
        reversible = "reversible" if e["reversible"] else "irreversible"
        print(
            f"    [{e['idx']}] {e['tool']}({e['args']})"
            f"  {reversible}  ->  {e['status']}"
        )
    print(
        "    (APPLIED = ran live; COMPENSATED = the backward fold restored it "
        "via ROLLBACK TO SAVEPOINT)"
    )


# --- 5. EMIT (clip-source) --------------------------------------------------
#
# Two animate paths share one source of truth: the persisted journal.
#   (a) the inspector reads JOURNAL_DB directly and animates it live;
#   (b) emit_clip_source distils the same journal into a small player-ready dict
#       — the same {title, situation, events, verdict} shape the original
#       dogfood capture.py --emit-demo produced, but read straight off the
#       journal instead of an agent transcript (no model run needed).


def emit_clip_source(
    journal: AuditJournal,
    txn_id: str,
    *,
    title: str,
    situation: str,
    before: dict[str, int],
    after: dict[str, int],
) -> dict:
    """Distil one transaction's journal into a player-ready clip-source dict.

    Events walk the journal in order (the live phase), then — if any effect was
    unwound — append the commit/rollback fold derived from final statuses. This
    is the journal-native version of capture.py's ``build_demo_events``: it
    needs no transcript because the journal already *is* the ordered record.
    """
    effects = journal.get_effects(txn_id)
    record = journal.get_transaction(txn_id)

    events: list[dict] = [{"k": "say", "text": situation}]
    for e in effects:
        kind = "applied" if e["reversible"] else "staged"
        events.append(
            {
                "k": kind,
                "idx": e["idx"],
                "tool": e["tool"],
                "res": e["resource"],
                "args": e["args"],
            }
        )

    # The backward fold: any effect whose final status is COMPENSATED was undone.
    undone = [e["idx"] for e in effects if e["status"] == "COMPENSATED"]
    if undone:
        events.append({"k": "phase", "text": "rollback() — folding the journal backward"})
        events.append({"k": "restore", "idxs": undone})

    rolled_back = record["state"] == "ROLLED_BACK"
    return {
        "title": title,
        "tab": "undo",
        "situation": situation,
        "events": events,
        "before": before,
        "after": after,
        "verdict": {
            "kind": "contained" if rolled_back else "committed",
            "big": "CONTAINED — ROLLED BACK" if rolled_back else "COMMITTED",
            "narr": (
                f"{sum(before.values()):,} rows touched live, "
                f"{len(undone)} effect(s) unwound by ROLLBACK TO SAVEPOINT, "
                f"0 rows lost."
            ),
        },
    }


# --- the run ----------------------------------------------------------------


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Fresh journal every run so the inspector shows only this run's story.
    for sib in (JOURNAL_DB, Path(str(JOURNAL_DB) + "-wal"), Path(str(JOURNAL_DB) + "-shm")):
        sib.unlink(missing_ok=True)

    # The real backend. isolation_level=None hands all BEGIN/SAVEPOINT/COMMIT/
    # ROLLBACK control to the adapter — Pherix owns the transaction bracket.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    seed_orders(conn, N_ORDERS)

    before = status_histogram(conn)
    print("PHERIX — UNDO (the wrong-but-allowed write, rolled back live)")
    print("=" * 70)
    print(f"\n  seeded {N_ORDERS:,} real orders")
    print(f"  before : {_fmt_hist(before)}")
    print(f"  voided : {count_voided(conn)}")

    # The journal persists to a SQLite file so the inspector can render it.
    journal = AuditJournal(str(JOURNAL_DB))
    adapters = {"sql": SQLiteAdapter(conn)}

    print("\n  agent runs its plan: 'void the stale orders'")
    with agent_txn(adapters, audit=journal, actor="orders-agent") as txn:
        affected = agent_body()
        # MID-TRANSACTION: the write has executed LIVE and is journalled.
        mid = status_histogram(conn)
        print(f"  -> the write ran live and touched {affected:,} rows")
        print(f"  mid    : {_fmt_hist(mid)}")
        print(f"  voided : {count_voided(conn):,}   <- every order is now 'void' (wrong!)")
        print("\n  the agent (or a reviewer) catches it — rolling the whole step back")
        txn.rollback()

    txn_id = txn.txn_id
    after = status_histogram(conn)
    print(f"\n  after  : {_fmt_hist(after)}")
    print(f"  voided : {count_voided(conn)}")

    # The proof: the after-state equals the before-state, exactly.
    lost = sum(before.values()) - sum(after.values())
    restored = before == after
    print("\n  RESULT")
    print(f"    {sum(before.values()):,} rows -> {lost} lost")
    print(f"    wrong write fully reversed by ROLLBACK TO SAVEPOINT : {restored}")
    assert restored, "rollback must restore the exact before-state"
    assert count_voided(conn) == 0, "no row should remain 'void' after rollback"

    narrate(conn, journal, txn_id)

    # EMIT: clip-source JSON (the alternative animate path).
    clip = emit_clip_source(
        journal,
        txn_id,
        title="Undo a wrong-but-allowed bulk write",
        situation="Agent means to void a few stale orders; its predicate voids all 40,000.",
        before=before,
        after=after,
    )
    CLIP_JSON.write_text(json.dumps(clip, indent=2))

    journal.close()
    conn.close()

    print("\n  ANIMATE THIS RUN")
    print("    live console (polls the journal, animates the unwind):")
    print(f"      python -m pherix.inspector --db {JOURNAL_DB}")
    print("      # then open http://127.0.0.1:8765")
    print("    clip-source JSON (player payload):")
    print(f"      {CLIP_JSON}")


if __name__ == "__main__":
    main()
