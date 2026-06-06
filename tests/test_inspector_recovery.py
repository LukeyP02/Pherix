"""JournalReader.recovery() — the reconciliation queue, a fold over the journal.

The undo guarantee's failure mode: when a backward fold can't complete (a
compensator missing or itself failing), the txn settles PARTIAL / STUCK and the
effects it already applied stay APPLIED — live side effects stranded outside any
committed transaction. recovery() gathers them as a pure traversal.

Pinned against the seeded demo journal, whose STUCK story is exactly this case:

  * txn-stuck-payout04  STUCK
      idx0 debit_ledger  sql  reversible   COMPENSATED  (cleanly reversed)
      idx1 send_payout   http irreversible APPLIED      (the dangling side effect)
      idx2 notify_vendor http irreversible FAILED       (never took effect)

The clean ROLLED_BACK story (txn-rollback-rel02, both effects COMPENSATED) must
NOT enter the queue — nothing stranded. Every test is offline: a seeded or
hand-built SQLite journal, no agent.

These all fail against the prior commit: there was no recovery() at all.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.inspector.reader import JournalReader
from pherix.inspector.seed import seed_demo_journal


@pytest.fixture
def reader(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    r = JournalReader(path)
    yield r
    r.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- the seeded STUCK story is the canonical queue entry ---------------------


def test_queue_holds_exactly_the_stuck_txn(reader: JournalReader):
    """Of the seed's seven txns, only the STUCK one needs reconciliation. The
    clean ROLLED_BACK (all COMPENSATED) is correctly absent — nothing stranded."""
    rec = reader.recovery()
    assert [q["txn_id"] for q in rec["queue"]] == ["txn-stuck-payout04"]
    entry = rec["queue"][0]
    assert entry["state"] == "STUCK"
    assert entry["tone"] == "error"


def test_dangling_is_the_surviving_applied_effect(reader: JournalReader):
    """send_payout fired (APPLIED) and the unwind never reached it — it is the
    one dangling effect, and it is irreversible: the worst case, manual undo."""
    entry = reader.recovery()["queue"][0]
    assert [d["tool"] for d in entry["dangling"]] == ["send_payout"]
    payout = entry["dangling"][0]
    assert payout["idx"] == 1
    assert payout["status"] == "APPLIED"
    assert payout["reversible"] is False
    assert payout["writes"] == []  # send_payout records no write key
    assert entry["dangling_count"] == 1
    assert entry["irreversible_dangling"] == 1


def test_reversed_and_failed_buckets(reader: JournalReader):
    """debit_ledger was cleanly COMPENSATED (already undone); notify_vendor
    FAILED and never took effect — neither is dangling, both carried."""
    entry = reader.recovery()["queue"][0]
    assert [r["tool"] for r in entry["reversed"]] == ["debit_ledger"]
    debit = entry["reversed"][0]
    assert debit["status"] == "COMPENSATED"
    assert debit["reversible"] is True
    # write_keys normalise to {resource, key, version}
    assert debit["writes"] == [
        {"resource": "sql", "key": ["ledger", "acct-19"], "version": 41}
    ]
    assert [f["tool"] for f in entry["failed"]] == ["notify_vendor"]
    assert entry["failed"][0]["status"] == "FAILED"


def test_totals_count_dangling_across_the_queue(reader: JournalReader):
    rec = reader.recovery()
    assert rec["totals"] == {
        "transactions": 1,
        "dangling_effects": 1,
        "irreversible_dangling_effects": 1,
    }


def test_scope_states_and_caveat_travel_with_payload(reader: JournalReader):
    scope = reader.recovery()["scope"]
    assert scope["incomplete_unwind_states"] == ["PARTIAL", "STUCK"]
    assert "rolled_back" in scope["also_included"]
    # The honest boundary: dangling is read off recorded status, not a live probe.
    assert "NOT a live probe" in scope["caveat"]


def test_seeded_entry_carries_actor_when_column_present(reader: JournalReader):
    """The current schema has the actor column (all NULL in the seed), so each
    dangling effect carries an explicit actor key — None here, but present."""
    payout = reader.recovery()["queue"][0]["dangling"][0]
    assert "actor" in payout
    assert payout["actor"] is None


# --- empty journal -----------------------------------------------------------


def test_empty_journal_yields_an_empty_queue(tmp_path: Path):
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()  # schema only, no rows
    with JournalReader(path) as r:
        rec = r.recovery()
    assert rec["queue"] == []
    assert rec["totals"] == {
        "transactions": 0,
        "dangling_effects": 0,
        "irreversible_dangling_effects": 0,
    }


# --- the anomaly path: a ROLLED_BACK txn that left an applied effect ----------


def _seed_one(path: str, txn_id: str, state: str, effects: list[tuple]) -> None:
    """Hand-write one txn + its effects through the current schema.

    ``effects`` items are ``(idx, tool, reversible, status)``.
    """
    AuditJournal(path).close()  # create current schema
    con = sqlite3.connect(path)
    now = _now()
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES (?, ?, ?, ?, 0)",
        (txn_id, state, now, now),
    )
    for idx, tool, reversible, status in effects:
        con.execute(
            "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, "
            "reversible, status, args, read_keys, write_keys, ts) "
            "VALUES (?, ?, ?, ?, 'sql', ?, ?, '{}', '[]', '[]', ?)",
            (txn_id, idx, f"e{idx}", tool, int(reversible), status, now),
        )
    con.commit()
    con.close()


def test_rolled_back_with_a_surviving_applied_effect_is_surfaced(tmp_path: Path):
    """A ROLLED_BACK txn should hold nothing applied — if it does, that's an
    integrity anomaly the queue must surface rather than hide."""
    path = str(tmp_path / "anomaly.db")
    _seed_one(
        path, "t-anom", "ROLLED_BACK",
        [(0, "w_one", True, "COMPENSATED"), (1, "w_two", True, "APPLIED")],
    )
    with JournalReader(path) as r:
        rec = r.recovery()
    assert [q["txn_id"] for q in rec["queue"]] == ["t-anom"]
    assert [d["tool"] for d in rec["queue"][0]["dangling"]] == ["w_two"]


def test_clean_rolled_back_is_not_queued(tmp_path: Path):
    """The mirror of the anomaly: a ROLLED_BACK whose every effect is
    COMPENSATED is clean — nothing to reconcile, absent from the queue."""
    path = str(tmp_path / "clean.db")
    _seed_one(
        path, "t-clean", "ROLLED_BACK",
        [(0, "w_one", True, "COMPENSATED"), (1, "w_two", True, "COMPENSATED")],
    )
    with JournalReader(path) as r:
        assert r.recovery()["queue"] == []


# --- ordering: freshest incident on top --------------------------------------


def test_queue_orders_most_recently_updated_first(tmp_path: Path):
    """Two STUCK incidents, distinct updated_at — the newer is on top."""
    path = str(tmp_path / "order.db")
    AuditJournal(path).close()
    con = sqlite3.connect(path)
    for txn_id, updated in [("t-old", "2026-01-01T00:00:00+00:00"),
                            ("t-new", "2026-06-01T00:00:00+00:00")]:
        con.execute(
            "INSERT INTO transactions (txn_id, state, created_at, updated_at, "
            "dry_run) VALUES (?, 'STUCK', ?, ?, 0)",
            (txn_id, updated, updated),
        )
        con.execute(
            "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, "
            "reversible, status, args, read_keys, write_keys, ts) "
            "VALUES (?, 0, 'e', 'w', 'sql', 0, 'APPLIED', '{}', '[]', '[]', ?)",
            (txn_id, updated),
        )
    con.commit()
    con.close()
    with JournalReader(path) as r:
        rec = r.recovery()
    assert [q["txn_id"] for q in rec["queue"]] == ["t-new", "t-old"]


# --- NULL-tolerant degradation: a journal predating the actor column ----------


def test_recovery_on_pre_actor_journal_omits_actor(tmp_path: Path):
    """A journal written before the effects.actor column existed must still
    fold into the queue — the per-effect actor key is simply omitted, not a
    crash. The NULL-tolerant degradation guard, mirrored from reliability()."""
    path = str(tmp_path / "ancient.db")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            replayed_from TEXT, dry_run INTEGER NOT NULL DEFAULT 0, client_id TEXT);
        CREATE TABLE effects (txn_id TEXT NOT NULL, idx INTEGER NOT NULL,
            effect_id TEXT NOT NULL, tool TEXT NOT NULL, resource TEXT NOT NULL,
            reversible INTEGER NOT NULL, status TEXT NOT NULL, args TEXT NOT NULL,
            snapshot TEXT, result TEXT, read_keys TEXT NOT NULL DEFAULT '[]',
            write_keys TEXT NOT NULL DEFAULT '[]', ts TEXT NOT NULL,
            PRIMARY KEY (txn_id, idx));
        """
    )
    now = _now()
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES ('t1', 'STUCK', ?, ?, 0)",
        (now, now),
    )
    con.execute(
        "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, reversible, "
        "status, args, read_keys, write_keys, ts) "
        "VALUES ('t1', 0, 'e', 'wipe', 'http', 0, 'APPLIED', '{}', '[]', '[]', ?)",
        (now,),
    )
    con.commit()
    con.close()

    with JournalReader(path) as r:
        rec = r.recovery()
    assert [q["txn_id"] for q in rec["queue"]] == ["t1"]
    dangling = rec["queue"][0]["dangling"][0]
    assert dangling["tool"] == "wipe"
    assert "actor" not in dangling  # column absent → key omitted, no crash
