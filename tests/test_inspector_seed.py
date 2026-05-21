"""The demo seeder produces the journal the inspector must render.

Pins that each of the six stories lands with the right terminal state and
effect statuses — these are the fixtures every other inspector test and the
operator's demo depend on, so a drift in the seeder should fail loudly here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pherix.inspector.reader import JournalReader
from pherix.inspector.seed import seed_demo_journal


def test_seed_writes_seven_stories(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    summary = seed_demo_journal(path)
    assert summary["transactions"] == 7
    with JournalReader(path) as r:
        assert r.stats()["txn_total"] == 7


def test_seeded_states(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        states = {t["txn_id"]: t["state"] for t in r.list_transactions()}
    assert states["txn-clean-deploy01"] == "COMMITTED"
    assert states["txn-rollback-rel02"] == "ROLLED_BACK"
    assert states["txn-gated-charge03"] == "STAGED"
    assert states["txn-stuck-payout04"] == "STUCK"
    assert states["txn-dryrun-plan05"] == "COMMITTED"


def test_rollback_story_is_all_compensated(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        tl = r.get_timeline("txn-rollback-rel02")
    assert all(e["status"] == "COMPENSATED" for e in tl["effects"])
    assert all(e["undone"] for e in tl["effects"])


def test_gated_story_has_irreversible_gated_effect(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        tl = r.get_timeline("txn-gated-charge03")
    charge = next(e for e in tl["effects"] if e["tool"] == "charge_card")
    assert charge["status"] == "GATED"
    assert charge["reversible"] is False


def test_dry_run_story_flagged(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        assert r.get_timeline("txn-dryrun-plan05")["transaction"]["dry_run"] is True


def test_attributed_stories_carry_client_id(tmp_path: Path):
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        clients = {
            t["txn_id"]: t["client_id"]
            for t in r.list_transactions()
            if t["client_id"]
        }
    assert clients == {
        "txn-clientA-q06": "claude-code",
        "txn-clientB-w07": "cursor-agent",
    }


def test_seeded_journal_is_a_plain_audit_db(tmp_path: Path):
    """The seeder writes the real schema — no inspector-only columns."""
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    con.close()
    assert {"transactions", "effects"} <= tables
