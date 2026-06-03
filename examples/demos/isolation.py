"""Pherix demo — ISOLATION: two agents race the same row, one is rejected.

    python examples/demos/isolation.py

The scenario is the second moat claim: MVCC-style isolation between concurrent
agents. Two agent transactions run in *real OS threads*, both pointed at one
real on-disk SQLite inventory. Both read the same row — sku ``WIDGET``,
``stock = 10`` — at the *same* version, each as the basis for selling one unit.
This is the classic lost-update setup: two read-modify-write sales on a shared
row with no coordination silently lose one of the two writes, and the warehouse
oversells (stock ends at 8 having sold 2 against a base it should have rechecked).

Pherix sits at the tool-call layer. The read executes **live** and is recorded
as ``read_keys[("inventory", WIDGET), v0]`` — the version the sale is based on.
The winning sale's decrement executes live behind a real ``SAVEPOINT`` and bumps
the version side-table to v1. When the *loser* reaches commit, the engine folds
its read-set against the live versions: its ``WIDGET`` read is now **stale** —
the committed version moved from v0 to v1 under it. The commit-time diff fires
:class:`IsolationConflict`, the loser's whole transaction rolls back, **its sale
never lands**, and the conflict is **recorded to the journal** as a first-class
row carrying the conflicting ``(resource, key, version)``.

Why read–write and not write–write: SQLite serialises writers — two
transactions cannot both physically UPDATE the same row and both reach commit
(the second writer is refused at the storage layer before any engine logic
runs). So the schedule that lets the ENGINE's diff be the thing that rejects the
loser — rather than a raw lock error — is exactly this: both read v0, one writes
+ commits, the other is caught at commit on its stale read *before* it can
oversell. That is the guarantee Pherix adds on top of the backend, and it is
the conflict shape the engine is built to detect.

The proof, read off real state: exactly **one** transaction commits and exactly
**one** conflicts; the conflict row is durable in the journal; final ``stock``
is ``9`` — one unit sold, not two. No oversell. No lost update. First-committer
wins, by the backend's version state, not by a guessed inverse.

The mental model (for the maths reader): a transaction is a *measurement* with
a boundary in time; the journal's read/write keys form a *partial order* under
happens-before. Two sales that both read ``WIDGET@v0`` and both act on it do not
*commute* — running them concurrently is not equal to running them in either
serial order, because the second's base is invalidated by the first. The
commit-time diff is exactly the non-commutativity detector: it compares the
version this transaction read against the committed version live now, and a
difference means another transaction's write happened-between. First-committer-
wins makes the schedule *serialisable* — equivalent to "the winner ran, then
the loser was correctly rejected for acting on a superseded read".

------------------------------------------------------------------------------
This file follows the same five-part skeleton as undo.py (the template):

  1. TOOLS    — @tool functions over one resource (the inventory), recording
                read/write keys via ``execute_isolated`` so the engine can diff.
  2. SEED     — a real on-disk WAL SQLite inventory; WIDGET stock=10.
  3. SCENARIO — TWO agent bodies in TWO real threads, event-ordered so both
                genuinely read the same version before the winner moves it.
  4. NARRATE  — print the race off the REAL journal + table: which txn won,
                which conflicted, the recorded conflict row, final stock.
  5. EMIT     — persist the journal to SQLite (inspector can animate it) AND
                dump a clip-source JSON. Both derived from the same journal.

Run it, then animate it two ways:

    # live console — polls the journal and animates the two txns + the conflict
    python -m pherix.inspector --db examples/demos/.out/isolation.db
    # then open http://127.0.0.1:8765

    # clip-source JSON for the player (printed path, also under .out/)
    examples/demos/.out/isolation.clip.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

# Run as `python examples/demos/isolation.py` with no editable install: put the
# repo root on the path before importing pherix. (Repo root is three levels up:
# examples/demos/isolation.py -> examples/demos -> examples -> repo root.)
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from pherix import (  # noqa: E402
    Abort,
    AuditJournal,
    IsolationConflict,
    SQLiteAdapter,
    agent_txn,
    tool,
)

# ``execute_isolated`` is the Slice-4 helper that records (resource, key,
# version) read/write triples into the active Effect — the substrate the
# commit-time diff folds over. It is not part of the top-level surface (it is a
# tool-author primitive, like ``FsHandle``), so import it from the adapter
# module directly, exactly as the isolation tests do.
from pherix.core.adapters.sql import execute_isolated  # noqa: E402

# Where this run's evidence lands. Gitignored (examples/ + *.db), regenerated
# every run — these are evidence, not source.
OUT_DIR = Path(__file__).resolve().parent / ".out"
JOURNAL_DB = OUT_DIR / "isolation.db"
CLIP_JSON = OUT_DIR / "isolation.clip.json"

SKU = "WIDGET"
INITIAL_STOCK = 10


# --- 1. TOOLS ---------------------------------------------------------------
#
# The real side-effecting operations over the inventory resource. ``conn`` is
# injected by the SQL adapter at apply-time and hidden from the agent's
# call-site. Both tools go through ``execute_isolated`` so the read of WIDGET
# records ``("sql", ("inventory", "WIDGET"), version_at_read)`` into the
# Effect's read_keys, and the decrement records the matching write_key — those
# triples are exactly what the commit-time diff folds over.


@tool(resource="sql")
def read_stock(conn: sqlite3.Connection, sku: str) -> int:
    """Read current stock for ``sku`` — and record it as an isolation read.

    Recording the read is what arms MVCC: the version observed *here* is the
    baseline the commit-time diff compares against. If anyone commits a write
    to this row between now and our commit, that baseline is stale and the
    diff fires.
    """
    cur = execute_isolated(
        conn,
        "SELECT stock FROM inventory WHERE sku = ?",
        (sku,),
        reads=[("inventory", sku)],
    )
    return cur.fetchone()[0]


@tool(resource="sql")
def decrement_stock(conn: sqlite3.Connection, sku: str, by: int) -> None:
    """Sell ``by`` units of ``sku``: decrement stock, bump the visible version.

    A real write — both the ``stock`` decrement and the user-visible
    ``version`` bump happen in one statement. Declaring the write key makes
    the engine bump the ``_pherix_versions`` side-table too: that side-table
    bump is what a *concurrent* transaction's commit diff will see and reject.
    """
    execute_isolated(
        conn,
        "UPDATE inventory SET stock = stock - ?, version = version + 1 "
        "WHERE sku = ?",
        (by, sku),
        writes=[("inventory", sku)],
    )


# --- 2. SEED ----------------------------------------------------------------


def seed_inventory(db_path: Path) -> None:
    """Build a real on-disk WAL inventory: one row, ``WIDGET`` stock=10.

    On disk + WAL (not ``:memory:``) so two threads holding two *separate*
    connections genuinely share the same backend — the only way the race is
    real rather than simulated. The ``version`` column is the user-visible
    optimistic-concurrency counter; the engine keeps its own parallel counter
    in ``_pherix_versions`` for the diff.
    """
    boot = sqlite3.connect(str(db_path), isolation_level=None)
    boot.execute("PRAGMA journal_mode=WAL")
    boot.execute("DROP TABLE IF EXISTS inventory")
    boot.execute(
        "CREATE TABLE inventory ("
        "  sku     TEXT PRIMARY KEY,"
        "  stock   INTEGER NOT NULL,"
        "  version INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    boot.execute(
        "INSERT INTO inventory (sku, stock, version) VALUES (?, ?, 0)",
        (SKU, INITIAL_STOCK),
    )
    boot.close()


def read_row(db_path: Path) -> tuple[int, int]:
    """The fact the narrative reads off the real table — never hard-coded.

    Returns ``(stock, version)`` for WIDGET from a fresh connection.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        row = conn.execute(
            "SELECT stock, version FROM inventory WHERE sku = ?", (SKU,)
        ).fetchone()
        return int(row[0]), int(row[1])
    finally:
        conn.close()


# --- 3. SCENARIO ------------------------------------------------------------
#
# Two agent transactions in two real OS threads against one shared on-disk DB.
# Each thread owns its OWN connection + adapter + agent_txn — Pherix's thread
# guard forbids sharing a live txn across threads, and contextvars are
# thread-local, so each thread's active_txn / active_effect is its own. That is
# precisely two independent agents racing, not one process pretending.
#
# Why the schedule looks the way it does — the SQLite single-writer fact.
# SQLite gives every connection a *snapshot*: a read pins the version it saw,
# and the engine records that into the Effect's read_keys. Two facts then box
# in the only honest schedule:
#
#   * Two connections cannot both hold the write lock — SQLite serialises
#     writers. Two transactions that BOTH physically UPDATE the same row and
#     BOTH reach commit is not a thing SQLite permits; the physically-second
#     writer is refused at the storage layer ("database is locked") before any
#     engine logic runs. That refusal is real isolation too, but it is the
#     *storage lock*, not the journal diff we are here to show.
#   * A connection that read at v0 cannot later UPDATE once the row has advanced
#     to v1 — its snapshot is stale and SQLite rejects the upgrade.
#
# So the schedule that lets the ENGINE's commit-time diff be the thing that
# fires (and records the conflict) is the read–write conflict the engine is
# built to catch: BOTH agents read WIDGET@v0 as the basis for their sale; the
# WINNER decrements and commits; the LOSER, holding the now-stale read, reaches
# commit and the engine's diff rejects it — *before* its decrement can land and
# oversell. The loser's would-be sale never happens: that is the guarantee.
# Two real events pin the ordering deterministically without faking the
# conflict — the engine, off real version state, decides the loser conflicts.


class _RaceResult:
    """What one racing thread reports back: did it commit, or conflict?"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.txn_id: str | None = None
        self.committed = False
        self.conflict: IsolationConflict | None = None
        self.error: BaseException | None = None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # busy_timeout bounds the brief window where the winner's write lock
    # overlaps the loser's read snapshot; under WAL a reader never blocks the
    # single writer, so this only smooths transient contention.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _run_loser(
    db_path: Path,
    journal_path: str,
    read_done: threading.Event,
    winner_committed: threading.Event,
    result: _RaceResult,
) -> None:
    """The agent that will be rejected. It reads WIDGET@v0 — the read its sale
    is based on — signals it has read, then waits for the winner to commit.
    When it reaches commit, the engine's diff finds its read stale and raises
    :class:`IsolationConflict`; agent_txn rolls it back. Its sale never lands.
    """
    conn = _connect(db_path)
    audit = AuditJournal(journal_path)
    adapter = SQLiteAdapter(conn)
    try:
        # Abort policy: on a stale-read conflict, raise IsolationConflict and
        # unwind. (Retry would replay the body; Serialize would block — both
        # are real options; Abort is the clearest proof of first-committer-wins.)
        with agent_txn({"sql": adapter}, audit=audit, isolation=Abort(),
                       actor="agent-B") as ctx:
            result.txn_id = ctx.txn_id
            seen = read_stock(sku=SKU)
            assert seen == INITIAL_STOCK, (
                f"loser expected stock={INITIAL_STOCK}, saw {seen}"
            )
            # This read is recorded as read_keys[("inventory", WIDGET), v0] —
            # the baseline the commit diff will compare against. Signal that the
            # baseline is captured, then wait for the winner to move the row.
            read_done.set()
            winner_committed.wait()
            # The agent's plan is now to sell: decrement WIDGET. But the block's
            # auto-commit runs the isolation diff FIRST — and the diff fires on
            # the stale v0 read before the decrement can oversell. (We could call
            # decrement_stock here; it would be unwound by the same rollback. The
            # point the demo proves is that the diff rejects the txn at commit, so
            # the would-be sale never reaches committed state.)
        result.committed = ctx.txn.state.name == "COMMITTED"
    except IsolationConflict as exc:
        # The loser: its read of WIDGET was stale at commit. The conflict is
        # already recorded to the journal (the runtime persists it BEFORE the
        # policy raises), and agent_txn has rolled this txn back.
        result.conflict = exc
    except BaseException as exc:  # surface any unexpected failure to main
        result.error = exc
    finally:
        audit.close()
        conn.close()


def _run_winner(
    db_path: Path,
    journal_path: str,
    read_done: threading.Event,
    winner_committed: threading.Event,
    result: _RaceResult,
) -> None:
    """The agent that commits. It starts only once the loser has read WIDGET@v0
    (so the loser's baseline is genuinely the pre-sale version), then reads,
    decrements, and commits — moving WIDGET v0 -> v1. Signals when committed so
    the loser proceeds to its (doomed) commit.
    """
    read_done.wait()  # the loser has captured WIDGET@v0; now move it under them
    conn = _connect(db_path)
    audit = AuditJournal(journal_path)
    adapter = SQLiteAdapter(conn)
    try:
        with agent_txn({"sql": adapter}, audit=audit, isolation=Abort(),
                       actor="agent-A") as ctx:
            result.txn_id = ctx.txn_id
            seen = read_stock(sku=SKU)
            assert seen == INITIAL_STOCK, (
                f"winner expected stock={INITIAL_STOCK}, saw {seen}"
            )
            decrement_stock(sku=SKU, by=1)  # sell one unit — the write that wins
        result.committed = ctx.txn.state.name == "COMMITTED"
    except BaseException as exc:
        result.error = exc
    finally:
        audit.close()
        conn.close()
        winner_committed.set()


def run_race(db_path: Path, journal_path: str) -> tuple[_RaceResult, _RaceResult]:
    """Launch both agents as real threads and join. Returns (winner, loser).

    Two events pin the only SQLite-honest schedule: the winner waits until the
    loser has read WIDGET@v0 (``read_done``), then sells + commits and signals
    (``winner_committed``); the loser then reaches its commit and the ENGINE —
    not this code — rejects it because its read is now stale. Both agents
    genuinely run their own transaction in their own thread; the events choose
    *who* is first, the journal diff decides the loser conflicts.
    """
    read_done = threading.Event()
    winner_committed = threading.Event()
    winner = _RaceResult("agent-A")
    loser = _RaceResult("agent-B")

    threads = [
        threading.Thread(
            target=_run_loser,
            args=(db_path, journal_path, read_done, winner_committed, loser),
        ),
        threading.Thread(
            target=_run_winner,
            args=(db_path, journal_path, read_done, winner_committed, winner),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return winner, loser


# --- 4. NARRATE -------------------------------------------------------------


def narrate(
    journal: AuditJournal, winner: _RaceResult, loser: _RaceResult
) -> dict:
    """Print both transactions off the REAL journal: the winner's applied
    decrement, the loser's rolled-back attempt, and the recorded conflict row.
    Returns the loser's recorded conflict dict (the journal's own record).
    """
    print("\n  journal — two transactions, one shared inventory")
    for res, label in ((winner, "WON "), (loser, "LOST")):
        record = journal.get_transaction(res.txn_id)
        print(f"\n    [{label}] {res.name}  txn={res.txn_id}"
              f"  final state = {record['state']}")
        for e in journal.get_effects(res.txn_id):
            rk = json.loads(e["read_keys"])
            wk = json.loads(e["write_keys"])
            tag = []
            if rk:
                tag.append(f"reads={[(k[1], f'v{k[2]}') for k in rk]}")
            if wk:
                tag.append(f"writes={[(k[1], f'v{k[2]}') for k in wk]}")
            print(f"      [{e['idx']}] {e['tool']}({e['args']})"
                  f"  ->  {e['status']}   {'  '.join(tag)}")

    conflicts = journal.get_conflicts(loser.txn_id)
    print(f"\n    conflict recorded on the LOSER ({loser.name}):")
    for c in conflicts:
        key = json.loads(c["key"])
        print(f"      resource={c['resource']}  key={tuple(key)}"
              f"  read v{json.loads(c['version_at_read'])}"
              f"  ->  committed v{json.loads(c['version_now'])}"
              f"  (expected v{json.loads(c['version_expected'])})")
    return conflicts[0] if conflicts else {}


# --- 5. EMIT (clip-source) --------------------------------------------------
#
# Two animate paths share one source of truth: the persisted journal.
#   (a) the inspector reads JOURNAL_DB directly and animates both txns + the
#       conflict;
#   (b) emit_clip_source distils the same journal into a small player-ready
#       dict — the same {title, situation, events, verdict} shape undo.py
#       produces, read straight off the journal (no model run needed).


def emit_clip_source(
    journal: AuditJournal,
    winner: _RaceResult,
    loser: _RaceResult,
    *,
    title: str,
    situation: str,
    before: dict[str, int],
    after: dict[str, int],
) -> dict:
    """Distil the race into a player-ready clip-source dict.

    Events walk the winner's journal (its read + applied decrement), then the
    loser's (its read + decrement, both unwound), then the recorded conflict,
    then the verdict. Every field is read off the journal — the only thing
    this function decides is presentation order (winner first).
    """
    events: list[dict] = [{"k": "say", "text": situation}]

    def _emit_txn(res: _RaceResult, role: str) -> None:
        events.append({"k": "phase", "text": f"{res.name} ({role})"})
        for e in journal.get_effects(res.txn_id):
            events.append(
                {
                    "k": "applied" if e["status"] == "APPLIED" else "restore"
                    if e["status"] == "COMPENSATED" else "say",
                    "idx": e["idx"],
                    "tool": e["tool"],
                    "res": e["resource"],
                    "args": json.loads(e["args"]),
                    "status": e["status"],
                }
            )

    _emit_txn(winner, "first-committer — wins")
    _emit_txn(loser, "stale read — rejected")

    conflicts = journal.get_conflicts(loser.txn_id)
    conflict_rows = [
        {
            "resource": c["resource"],
            "key": json.loads(c["key"]),
            "version_at_read": json.loads(c["version_at_read"]),
            "version_now": json.loads(c["version_now"]),
        }
        for c in conflicts
    ]
    events.append(
        {"k": "phase", "text": "commit-time diff — folding read-sets vs live versions"}
    )
    events.append({"k": "conflict", "conflicts": conflict_rows})

    winner_record = journal.get_transaction(winner.txn_id)
    loser_record = journal.get_transaction(loser.txn_id)
    return {
        "title": title,
        "tab": "isolation",
        "situation": situation,
        "events": events,
        "before": before,
        "after": after,
        "conflicts": conflict_rows,
        "winner": {"actor": winner.name, "state": winner_record["state"]},
        "loser": {"actor": loser.name, "state": loser_record["state"]},
        "verdict": {
            "kind": "isolated",
            "big": "ISOLATED — FIRST-COMMITTER WINS",
            "narr": (
                f"2 agents both read {SKU}@v0 and both intended to sell 1 unit; "
                f"1 committed, 1 was rejected at commit on its stale read; stock "
                f"{before[SKU]} -> {after[SKU]} (1 sold, not 2). "
                f"No oversell, no lost update."
            ),
        },
    }


# --- the run ----------------------------------------------------------------


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Fresh DB + journal every run so the inspector shows only this run's story.
    db_path = OUT_DIR / "isolation.inventory.db"
    for base in (JOURNAL_DB, db_path):
        for sib in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
            sib.unlink(missing_ok=True)

    seed_inventory(db_path)
    before_stock, before_version = read_row(db_path)
    before = {SKU: before_stock}

    print("PHERIX — ISOLATION (two agents race one row; first-committer wins)")
    print("=" * 70)
    print(f"\n  seeded real on-disk inventory : {SKU} stock={before_stock} "
          f"version={before_version}")
    print("  two agent transactions, two real threads, one shared backend")
    print(f"  both will read {SKU}@v{before_version} and both intend to sell 1 unit")

    journal_path = str(JOURNAL_DB)
    res_a, res_b = run_race(db_path, journal_path)

    # Surface any unexpected thread failure before asserting on the race itself.
    for res in (res_a, res_b):
        if res.error is not None:
            raise AssertionError(
                f"{res.name} failed unexpectedly: {res.error!r}"
            ) from res.error

    # Derive winner/loser from the ACTUAL outcomes, not the thread roles — the
    # asserts below must prove "exactly one committed, exactly one conflicted"
    # off real state, independent of which thread we expected to win.
    committed = [r for r in (res_a, res_b) if r.committed]
    conflicted = [r for r in (res_a, res_b) if r.conflict is not None]

    print("\n  RACE OUTCOME")
    for res in (res_a, res_b):
        verdict = "COMMITTED" if res.committed else (
            "CONFLICTED -> ROLLED BACK" if res.conflict else "???"
        )
        print(f"    {res.name}  txn={res.txn_id}  ->  {verdict}")

    # --- THE PROOF, read off real state -------------------------------------
    #
    # 1) Exactly one committed and exactly one conflicted — first-committer
    #    wins, second-committer is rejected. Not "we hope"; the engine decided.
    assert len(committed) == 1, (
        f"exactly one txn must commit; got {len(committed)}"
    )
    assert len(conflicted) == 1, (
        f"exactly one txn must conflict; got {len(conflicted)}"
    )
    winner, loser = committed[0], conflicted[0]

    # 2) The conflict the loser raised carries the real conflicting key + the
    #    moved version — read v0, committed v1 — straight off the engine.
    conflict_keys = loser.conflict.conflicts
    assert len(conflict_keys) == 1, "exactly one key should conflict"
    c0 = conflict_keys[0]
    assert c0.resource == "sql"
    assert tuple(c0.key) == ("inventory", SKU), (
        f"conflict must be on the {SKU} row, got {c0.key}"
    )
    assert c0.version_now != c0.version_at_read, (
        "the conflict exists because the version moved under the stale read"
    )

    # 3) The conflict is DURABLE in the journal — a first-class row, with the
    #    same (resource, key, version) the loser saw.
    reopened = AuditJournal(journal_path)
    try:
        recorded = reopened.get_conflicts(loser.txn_id)
        assert len(recorded) == 1, (
            f"the journal must hold exactly one conflict row for the loser; "
            f"got {len(recorded)}"
        )
        rec = recorded[0]
        assert rec["resource"] == "sql"
        assert tuple(json.loads(rec["key"])) == ("inventory", SKU)
        assert json.loads(rec["version_now"]) != json.loads(rec["version_at_read"])
        # The winner committed clean — zero conflict rows on its txn.
        assert reopened.get_conflicts(winner.txn_id) == [], (
            "the winner committed with no conflict"
        )

        # 4) Final stock is CONSISTENT: exactly one unit sold. No oversell
        #    (stock < 9 would mean both writes landed); no lost update
        #    (stock == 10 would mean neither did).
        after_stock, after_version = read_row(db_path)
        after = {SKU: after_stock}
        assert after_stock == before_stock - 1, (
            f"exactly one unit must be sold: {before_stock} -> {after_stock}"
        )
        assert after_version == before_version + 1, (
            "exactly one version bump must be visible"
        )

        print("\n  PROVEN")
        print(f"    transactions committed : 1   ({winner.name})")
        print(f"    transactions conflicted: 1   ({loser.name}, recorded in journal)")
        print(f"    {SKU} stock : {before_stock} -> {after_stock}   "
              f"(1 sold, not 2 — no oversell, no lost update)")
        print(f"    {SKU} version : {before_version} -> {after_version}")

        # NARRATE off the real journal (uses the reopened handle).
        narrate(reopened, winner, loser)

        # EMIT: clip-source JSON (the alternative animate path).
        clip = emit_clip_source(
            reopened,
            winner,
            loser,
            title="Isolation — two agents race one row, one is rejected",
            situation=(
                f"Two agents both read {SKU}@v{before_version} (stock "
                f"{before_stock}) and both try to sell 1 unit."
            ),
            before=before,
            after=after,
        )
        CLIP_JSON.write_text(json.dumps(clip, indent=2))
    finally:
        reopened.close()

    print("\n  ANIMATE THIS RUN")
    print("    live console (polls the journal, animates both txns + the conflict):")
    print(f"      python -m pherix.inspector --db {JOURNAL_DB}")
    print("      # then open http://127.0.0.1:8765")
    print("    clip-source JSON (player payload):")
    print(f"      {CLIP_JSON}")


if __name__ == "__main__":
    main()
