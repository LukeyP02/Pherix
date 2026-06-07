"""JournalReader approvals — the over-the-wire gate queue, a fold over the journal.

Prong #1's read side. The engine's ``approvals`` table records every irreversible
the gate held for an out-of-process ``approve(token)`` — PENDING while waiting,
APPROVED once a principal cleared it. :meth:`JournalReader.approvals` folds that
table into the queue an operator works (``pending``) and the cleared log
(``approved``); :meth:`JournalReader.get_approvals` attaches the same records to a
transaction's timeline. Both are pure traversals — no live state, nothing recomputed.

The seeded demo journal now carries one PENDING approval: the gated charge
(``charge_card``, an irreversible held at the gate) raised a request waiting on an
approve over the wire. The clear path (APPROVED + approver) is exercised on
hand-built journals, as is the NULL/absent-column degradation.

Every test is offline (a seeded or hand-built SQLite journal, no agent) and every
one fails against the prior commit — there was no approvals reader at all.
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


def _build(path: str, approvals: list[dict]) -> None:
    """A current-schema journal with one STAGED txn whose effects each carry an
    approval. Each ``approvals`` item is a dict with ``effect_id``/``token``/
    ``status`` and the optional ``approver``/``requested_at``/``approved_at``/
    ``actor``. One GATED effect is written per approval so the enrichment join
    has a row to attach (the LEFT-JOIN tolerance of a missing one is its own test).
    """
    AuditJournal(path).close()  # current schema, incl. the approvals table
    con = sqlite3.connect(path)
    now = _now()
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES ('t1', 'STAGED', ?, ?, 0)",
        (now, now),
    )
    for i, a in enumerate(approvals):
        if a.get("with_effect", True):
            con.execute(
                "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, "
                "reversible, status, args, read_keys, write_keys, actor, ts) "
                "VALUES ('t1', ?, ?, 'charge_card', 'http', 0, 'GATED', '{}', "
                "'[]', '[]', ?, ?)",
                (i, a["effect_id"], a.get("actor"), now),
            )
        con.execute(
            "INSERT INTO approvals (txn_id, effect_id, token, status, approver, "
            "requested_at, approved_at) VALUES ('t1', ?, ?, ?, ?, ?, ?)",
            (
                a["effect_id"],
                a["token"],
                a["status"],
                a.get("approver"),
                a.get("requested_at", now),
                a.get("approved_at"),
            ),
        )
    con.commit()
    con.close()


# --- the seeded PENDING is the canonical queue entry -------------------------


def test_seeded_pending_holds_the_gated_charge(reader: JournalReader):
    """The one seeded approval is the held irreversible — it is PENDING, in the
    queue, and the approved log is empty (no clear has landed)."""
    apr = reader.approvals()
    assert apr["totals"] == {"pending": 1, "approved": 0, "approvers": []}
    assert len(apr["pending"]) == 1
    assert apr["approved"] == []


def test_pending_record_is_enriched_with_the_gated_effect(reader: JournalReader):
    """The PENDING record carries the gated effect's identity (LEFT JOIN), so a
    console renders *what* is held without a second lookup."""
    held = reader.approvals()["pending"][0]
    assert held["status"] == "PENDING"
    assert held["approved"] is False
    assert held["tone"] == "pending"
    assert held["tool"] == "charge_card"
    assert held["resource"] == "http"
    assert held["reversible"] is False  # the irreversible the gate exists for
    assert held["approver"] is None
    assert held["approved_at"] is None
    assert held["token"]  # a non-empty over-the-wire handle
    assert held["txn_id"] == "txn-gated-charge03"
    assert held["txn_state"] == "STAGED"


def test_timeline_carries_the_pending_approval(reader: JournalReader):
    """The transaction's timeline carries its approvals beside the effects, the
    same way it carries conflicts — the gate lifecycle is a txn-level fact."""
    tl = reader.get_timeline("txn-gated-charge03")
    assert "approvals" in tl
    assert [a["tool"] for a in tl["approvals"]] == ["charge_card"]
    assert tl["approvals"][0]["status"] == "PENDING"
    # a txn with no gated irreversible has no approvals
    assert reader.get_timeline("txn-clean-deploy01")["approvals"] == []


def test_scope_and_caveat_travel_with_payload(reader: JournalReader):
    scope = reader.approvals()["scope"]
    assert scope["states"] == ["PENDING", "APPROVED"]
    # the honest boundary: an APPROVED row proves authorisation, not that the
    # held txn has since resumed and committed.
    assert "NOT that the held transaction has since resumed" in scope["caveat"]


def test_stats_surface_pending_and_total(reader: JournalReader):
    s = reader.stats()
    assert s["approvals_pending"] == 1
    assert s["approvals_total"] == 1


# --- the clear path: an APPROVED record --------------------------------------


def test_approved_record_lands_in_the_cleared_log(tmp_path: Path):
    """An APPROVED approval moves out of the pending queue into the cleared log,
    carrying the principal who approved it and when."""
    path = str(tmp_path / "approved.db")
    _build(path, [{
        "effect_id": "eff-1", "token": "tok-1", "status": "APPROVED",
        "approver": "role:risk-officer",
        "requested_at": "2026-06-01T00:00:00+00:00",
        "approved_at": "2026-06-01T00:05:00+00:00",
    }])
    with JournalReader(path) as r:
        apr = r.approvals()
    assert apr["pending"] == []
    assert len(apr["approved"]) == 1
    cleared = apr["approved"][0]
    assert cleared["status"] == "APPROVED"
    assert cleared["approved"] is True
    assert cleared["tone"] == "ok"
    assert cleared["approver"] == "role:risk-officer"
    assert cleared["approved_at"] == "2026-06-01T00:05:00+00:00"


def test_totals_count_distinct_approvers(tmp_path: Path):
    """Two clears by the same principal count once in ``approvers``; a third by
    another principal adds a second. Pending rows never contribute an approver."""
    path = str(tmp_path / "approvers.db")
    _build(path, [
        {"effect_id": "e1", "token": "t1", "status": "APPROVED",
         "approver": "alice", "approved_at": "2026-06-01T01:00:00+00:00"},
        {"effect_id": "e2", "token": "t2", "status": "APPROVED",
         "approver": "alice", "approved_at": "2026-06-01T02:00:00+00:00"},
        {"effect_id": "e3", "token": "t3", "status": "APPROVED",
         "approver": "bob", "approved_at": "2026-06-01T03:00:00+00:00"},
        {"effect_id": "e4", "token": "t4", "status": "PENDING"},
    ])
    with JournalReader(path) as r:
        apr = r.approvals()
    assert apr["totals"]["pending"] == 1
    assert apr["totals"]["approved"] == 3
    assert apr["totals"]["approvers"] == ["alice", "bob"]


# --- ordering: freshest on top -----------------------------------------------


def test_pending_orders_freshest_request_first(tmp_path: Path):
    """PENDING records order by request time, freshest hold on top."""
    path = str(tmp_path / "pending_order.db")
    _build(path, [
        {"effect_id": "old", "token": "t-old", "status": "PENDING",
         "requested_at": "2026-01-01T00:00:00+00:00"},
        {"effect_id": "new", "token": "t-new", "status": "PENDING",
         "requested_at": "2026-06-01T00:00:00+00:00"},
    ])
    with JournalReader(path) as r:
        pending = r.approvals()["pending"]
    assert [p["effect_id"] for p in pending] == ["new", "old"]


def test_approved_orders_freshest_clear_first(tmp_path: Path):
    """APPROVED records order by *approval* time (not request time) — the
    cleared log reads most-recently-cleared first."""
    path = str(tmp_path / "approved_order.db")
    _build(path, [
        {"effect_id": "early", "token": "t-e", "status": "APPROVED",
         "approver": "x", "requested_at": "2026-06-01T00:00:00+00:00",
         "approved_at": "2026-06-01T00:01:00+00:00"},
        {"effect_id": "late", "token": "t-l", "status": "APPROVED",
         "approver": "x", "requested_at": "2026-05-01T00:00:00+00:00",
         "approved_at": "2026-06-02T00:00:00+00:00"},
    ])
    with JournalReader(path) as r:
        approved = r.approvals()["approved"]
    # 'late' was requested earlier but cleared later → on top by approved_at.
    assert [a["effect_id"] for a in approved] == ["late", "early"]


# --- enrichment tolerance + actor axis ---------------------------------------


def test_actor_carried_when_effects_column_present(tmp_path: Path):
    """The gated effect's ``actor`` (on-whose-authority) rides with the approval
    when the effects table has the column — the actor axis of the gate."""
    path = str(tmp_path / "actor.db")
    _build(path, [{
        "effect_id": "e1", "token": "t1", "status": "PENDING",
        "actor": "role:admin",
    }])
    with JournalReader(path) as r:
        held = r.approvals()["pending"][0]
    assert held["actor"] == "role:admin"


def test_approval_without_a_matching_effect_still_surfaces(tmp_path: Path):
    """The enrichment is a LEFT JOIN: an approval whose effect row is absent
    still appears (its effect identity simply None), never a crash."""
    path = str(tmp_path / "orphan.db")
    _build(path, [{
        "effect_id": "ghost", "token": "t-ghost", "status": "PENDING",
        "with_effect": False,
    }])
    with JournalReader(path) as r:
        pending = r.approvals()["pending"]
    assert [p["effect_id"] for p in pending] == ["ghost"]
    assert pending[0]["tool"] is None
    assert pending[0]["reversible"] is None


# --- empty + NULL-tolerant degradation ---------------------------------------


def test_empty_journal_yields_an_empty_queue(tmp_path: Path):
    """A schema-only journal (the approvals table exists but is empty) folds to
    an empty queue and zero totals — not absent, not an error."""
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()
    with JournalReader(path) as r:
        apr = r.approvals()
        s = r.stats()
    assert apr["pending"] == []
    assert apr["approved"] == []
    assert apr["totals"] == {"pending": 0, "approved": 0, "approvers": []}
    # the caveat still travels even on an empty journal
    assert "Over-the-wire gate queue" in apr["scope"]["caveat"]
    assert s["approvals_pending"] == 0
    assert s["approvals_total"] == 0


def test_journal_predating_the_approvals_table_degrades(tmp_path: Path):
    """A journal whose schema has no approvals table at all (a pre-Prong-#1
    journal) degrades to the empty queue rather than failing to load —
    mirroring how the reader degrades for conflicts and verdicts."""
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
        "VALUES ('t1', 'STAGED', ?, ?, 0)",
        (now, now),
    )
    con.commit()
    con.close()
    with JournalReader(path) as r:
        apr = r.approvals()
        # get_timeline must also tolerate the missing table
        tl = r.get_timeline("t1")
        s = r.stats()
    assert apr["pending"] == [] and apr["approved"] == []
    assert apr["totals"] == {"pending": 0, "approved": 0, "approvers": []}
    assert tl["approvals"] == []
    assert s["approvals_pending"] == 0
    assert s["approvals_total"] == 0
