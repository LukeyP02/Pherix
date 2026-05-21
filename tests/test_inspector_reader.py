"""The inspector's read layer, tested hard.

Covers the four shapes the spec calls out — golden / empty / mid-flight /
STUCK / dry-run — plus the status→verdict derivation, every list filter, the
stats roll-up, and graceful handling of a journal that predates verdict
persistence (no ``verdicts`` table) and one written by a newer engine
(unknown status string). Fully offline: a seeded SQLite journal, no agent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState
from pherix.inspector.reader import JournalReader, effect_verdict, txn_summary
from pherix.inspector.seed import seed_demo_journal


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def demo_db(tmp_path: Path) -> str:
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    return path


@pytest.fixture
def reader(demo_db: str):
    r = JournalReader(demo_db)
    yield r
    r.close()


def _empty_db(tmp_path: Path) -> str:
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()  # schema only, no rows
    return path


# --- pure derivations -------------------------------------------------------


def test_effect_verdict_maps_every_known_status():
    assert effect_verdict("APPLIED")["tone"] == "ok"
    assert effect_verdict("STAGED")["tone"] == "pending"
    assert effect_verdict("GATED")["tone"] == "blocked"
    assert effect_verdict("COMPENSATED")["undone"] is True
    assert effect_verdict("FAILED")["tone"] == "error"


def test_effect_verdict_unknown_status_degrades_not_raises():
    v = effect_verdict("QUANTUM")  # a status from some future engine
    assert v["tone"] == "unknown"
    assert v["undone"] is False
    assert v["verdict"] == "quantum"


def test_txn_summary_known_and_unknown():
    assert txn_summary("STUCK")["tone"] == "error"
    assert txn_summary("COMMITTED")["tone"] == "ok"
    assert txn_summary("ROLLED_BACK")["tone"] == "undone"
    assert txn_summary("WAT")["tone"] == "unknown"


# --- stats ------------------------------------------------------------------


def test_stats_counts_the_seeded_journal(reader: JournalReader):
    s = reader.stats()
    assert s["txn_total"] == 7
    assert s["effect_total"] == 14
    assert s["txns_by_state"]["COMMITTED"] == 4
    assert s["txns_by_state"]["STUCK"] == 1
    assert s["txns_by_state"]["ROLLED_BACK"] == 1
    assert s["txns_by_state"]["STAGED"] == 1
    assert s["clients"] == ["claude-code", "cursor-agent"]
    assert "charge_card" in s["tools"]
    assert s["has_verdicts"] is False  # no verdicts table in a seeded journal


def test_stats_empty_journal(tmp_path: Path):
    with JournalReader(_empty_db(tmp_path)) as r:
        s = r.stats()
        assert s["txn_total"] == 0
        assert s["effect_total"] == 0
        assert s["clients"] == []
        assert s["tools"] == []


# --- list / filter ----------------------------------------------------------


def test_list_returns_all_newest_first(reader: JournalReader):
    txns = reader.list_transactions()
    assert len(txns) == 7
    # created_at descending
    times = [t["created_at"] for t in txns]
    assert times == sorted(times, reverse=True)


def test_list_summary_flags(reader: JournalReader):
    by_id = {t["txn_id"]: t for t in reader.list_transactions()}
    assert by_id["txn-gated-charge03"]["has_gate"] is True
    assert by_id["txn-rollback-rel02"]["has_compensation"] is True
    assert by_id["txn-rollback-rel02"]["is_rolled_back"] is True
    assert by_id["txn-stuck-payout04"]["is_stuck"] is True
    assert by_id["txn-stuck-payout04"]["has_failure"] is True
    assert by_id["txn-clean-deploy01"]["effect_count"] == 3
    assert by_id["txn-clean-deploy01"]["tone"] == "ok"


def test_list_filter_by_state(reader: JournalReader):
    stuck = reader.list_transactions(state="STUCK")
    assert [t["txn_id"] for t in stuck] == ["txn-stuck-payout04"]


def test_list_filter_by_client(reader: JournalReader):
    assert [t["txn_id"] for t in reader.list_transactions(client_id="cursor-agent")] == [
        "txn-clientB-w07"
    ]


def test_list_filter_by_tool_matches_containing_txn(reader: JournalReader):
    # charge_card appears in the gated txn and the dry-run txn.
    ids = {t["txn_id"] for t in reader.list_transactions(tool="charge_card")}
    assert ids == {"txn-gated-charge03", "txn-dryrun-plan05"}


def test_list_compliance_view_hides_dry_run(reader: JournalReader):
    ids = {t["txn_id"] for t in reader.list_transactions(include_dry_run=False)}
    assert "txn-dryrun-plan05" not in ids
    assert len(ids) == 6


def test_list_limit(reader: JournalReader):
    assert len(reader.list_transactions(limit=2)) == 2


def test_list_since_until_bounds(reader: JournalReader):
    everything = reader.list_transactions()
    midpoint = everything[3]["created_at"]
    newer = reader.list_transactions(since=midpoint)
    assert all(t["created_at"] >= midpoint for t in newer)


# --- timeline ---------------------------------------------------------------


def test_timeline_missing_txn_is_none(reader: JournalReader):
    assert reader.get_timeline("txn-does-not-exist") is None


def test_timeline_orders_effects_and_derives_verdicts(reader: JournalReader):
    tl = reader.get_timeline("txn-stuck-payout04")
    assert tl is not None
    assert [e["idx"] for e in tl["effects"]] == [0, 1, 2]
    verdicts = {e["tool"]: e["verdict"] for e in tl["effects"]}
    assert verdicts == {
        "debit_ledger": "compensated",
        "send_payout": "applied",
        "notify_vendor": "failed",
    }
    # the compensated effect is marked undone (struck through in the UI)
    debit = next(e for e in tl["effects"] if e["tool"] == "debit_ledger")
    assert debit["undone"] is True
    assert debit["reversible"] is True


def test_timeline_parses_keys_and_args(reader: JournalReader):
    tl = reader.get_timeline("txn-clean-deploy01")
    bump = next(e for e in tl["effects"] if e["tool"] == "bump_version")
    assert bump["args"] == {"to": "v2.4.0"}
    assert bump["write_keys"] == [["sql", ["releases", "current"], 12]]
    read = next(e for e in tl["effects"] if e["tool"] == "read_release")
    assert read["read_keys"] == [["sql", ["releases", "current"], 11]]


def test_timeline_gated_irreversible_reads_at_a_glance(reader: JournalReader):
    tl = reader.get_timeline("txn-gated-charge03")
    charge = next(e for e in tl["effects"] if e["tool"] == "charge_card")
    assert charge["status"] == "GATED"
    assert charge["tone"] == "blocked"
    assert charge["reversible"] is False
    # no verdicts table → empty per-rule list, derived verdict still present
    assert charge["policy_verdicts"] == []


def test_timeline_dry_run_flag_on_summary(reader: JournalReader):
    tl = reader.get_timeline("txn-dryrun-plan05")
    assert tl["transaction"]["dry_run"] is True


# --- mid-flight (OPEN txn, effects part-applied) ----------------------------


def test_mid_flight_open_transaction(tmp_path: Path):
    path = str(tmp_path / "midflight.db")
    journal = AuditJournal(path)
    t = Transaction(txn_id="txn-open01", state=TxnState.OPEN)
    t.effects = [
        Effect(txn_id=t.txn_id, index=0, tool="read_row", args={}, resource="sql",
               reversible=True, status=EffectStatus.APPLIED,
               ts=datetime.now(timezone.utc)),
        Effect(txn_id=t.txn_id, index=1, tool="stage_charge", args={"amt": 10},
               resource="http", reversible=False, status=EffectStatus.STAGED,
               ts=datetime.now(timezone.utc)),
    ]
    journal.record_transaction(t)
    for e in t.effects:
        journal.record_effect(e)
    journal.close()

    with JournalReader(path) as r:
        summ = r.list_transactions()[0]
        assert summ["state"] == "OPEN"
        assert summ["tone"] == "pending"
        tl = r.get_timeline("txn-open01")
        tones = [e["tone"] for e in tl["effects"]]
        assert tones == ["ok", "pending"]  # applied, then staged-pending


# --- newer-engine status (unknown) handled via raw injection ----------------


def test_unknown_status_row_renders_without_crashing(tmp_path: Path):
    path = str(tmp_path / "future.db")
    AuditJournal(path).close()
    # Inject a row with a status this reader version doesn't know.
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES ('txn-future', 'QUARANTINED', ?, ?, 0)",
        (datetime.now(timezone.utc).isoformat(),) * 2,
    )
    con.execute(
        "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, reversible, "
        "status, args, read_keys, write_keys, ts) "
        "VALUES ('txn-future', 0, 'eid', 'warp', 'sql', 1, 'SUPERPOSED', '{}', "
        "'[]', '[]', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    con.commit()
    con.close()

    with JournalReader(path) as r:
        summ = r.list_transactions()[0]
        assert summ["tone"] == "unknown"  # unknown txn state degrades
        tl = r.get_timeline("txn-future")
        assert tl["effects"][0]["tone"] == "unknown"
