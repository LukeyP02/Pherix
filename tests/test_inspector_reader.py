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
    assert s["has_verdicts"] is True  # the seeder writes per-rule verdicts


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


def test_timeline_surfaces_world_state_divergence(reader: JournalReader):
    """The keystone policy story: a cap that ALLOWS at stage but DENIES at
    commit because the running total moved. The inspector must show both
    phases on the same effect so the divergence reads at a glance."""
    tl = reader.get_timeline("txn-gated-charge03")
    charge = next(e for e in tl["effects"] if e["tool"] == "charge_card")
    phases = {(v["phase"], v["allow"]) for v in charge["policy_verdicts"]}
    assert ("stage", True) in phases   # allowed when planned
    assert ("commit", False) in phases  # denied when it would fire
    deny = next(v for v in charge["policy_verdicts"] if not v["allow"])
    assert deny["kind"] == "cap"
    assert "5000" in deny["reason"]


def test_timeline_no_verdicts_when_table_empty(tmp_path: Path):
    """A journal whose verdicts table has no rows for a txn → empty per-rule
    list; the status-derived verdict still carries the timeline."""
    path = str(tmp_path / "noverdicts.db")
    AuditJournal(path).close()
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES ('t1', 'COMMITTED', ?, ?, 0)",
        (datetime.now(timezone.utc).isoformat(),) * 2,
    )
    con.execute(
        "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, reversible, "
        "status, args, read_keys, write_keys, ts) "
        "VALUES ('t1', 0, 'e', 'w', 'sql', 1, 'APPLIED', '{}', '[]', '[]', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    con.commit()
    con.close()
    with JournalReader(path) as r:
        tl = r.get_timeline("t1")
        assert tl["effects"][0]["policy_verdicts"] == []
        assert tl["effects"][0]["verdict"] == "applied"  # derived still present


def test_timeline_dry_run_flag_on_summary(reader: JournalReader):
    tl = reader.get_timeline("txn-dryrun-plan05")
    assert tl["transaction"]["dry_run"] is True


# --- per-effect result→inputs provenance (informed_by) ----------------------
#
# The remaining provenance item: each effect's RESULT is annotated with the
# read_keys that informed it — explicit, per-effect, not implicit in journal
# order. ``lineage()`` carries this only for *writer* effects, in a separate
# graph; the timeline (where the ``result`` lives) gets the same intra-txn fold
# attached per effect. These tests fail against origin/main, which has no
# ``informed_by`` key on a timeline effect.


def test_timeline_effect_carries_informed_by_for_its_result(reader: JournalReader):
    """The seed's clean deploy is a real read→write: read_release (idx 0) reads
    releases/current v11, then bump_version (idx 1) writes v12. The write's
    result must be annotated with the read that informed it — explicitly, not
    left implicit in journal order."""
    tl = reader.get_timeline("txn-clean-deploy01")
    by_tool = {e["tool"]: e for e in tl["effects"]}

    # the writer is informed by the earlier read of the same key
    bump = by_tool["bump_version"]
    informers = [(i["tool"], i["resource"], i["key"], i["version"],
                  i["same_effect"]) for i in bump["informed_by"]]
    assert ("read_release", "sql", ["releases", "current"], 11, False) in informers

    # the reading effect itself lists its own read, flagged same_effect
    read = by_tool["read_release"]
    own = next(i for i in read["informed_by"] if i["tool"] == "read_release")
    assert own["same_effect"] is True
    assert (own["resource"], own["key"], own["version"]) == \
        ("sql", ["releases", "current"], 11)
    # v11's producing write predates this journal → external, honestly flagged
    assert own["produced_by"] is None
    assert own["produced_by_external"] is True


def test_timeline_informed_by_accumulates_prefix(reader: JournalReader):
    """informed_by is the happens-before prefix: an effect later in the txn
    inherits every earlier read. write_manifest (idx 2) reads nothing itself
    but still carries the read_release read that preceded it."""
    tl = reader.get_timeline("txn-clean-deploy01")
    by_tool = {e["tool"]: e for e in tl["effects"]}

    read = by_tool["read_release"]      # idx 0: one read, its own
    bump = by_tool["bump_version"]      # idx 1: no read of its own, inherits #0
    manifest = by_tool["write_manifest"]  # idx 2: still inherits #0

    assert len(read["informed_by"]) == 1
    assert [i["tool"] for i in bump["informed_by"]] == ["read_release"]
    assert [i["tool"] for i in manifest["informed_by"]] == ["read_release"]


def test_timeline_informed_by_empty_when_no_prior_read(reader: JournalReader):
    """A first effect that is itself a write with no read carries an empty
    informed_by — the fold invents no provenance. txn-rollback-rel02#0 is a
    bare write."""
    tl = reader.get_timeline("txn-rollback-rel02")
    first = tl["effects"][0]
    assert first["tool"] == "bump_version"
    assert first["informed_by"] == []


def test_timeline_informed_by_version_grounded_producer(tmp_path: Path):
    """When the value a read observed was written earlier IN THIS JOURNAL, the
    informing read names its producer (version-grounded), not 'external'. One
    txn writes prices/sku1 v5; a later txn reads v5 then writes a derived row —
    that derived write's result is informed by a read with produced_by set."""
    writer = Transaction(txn_id="t-writer", state=TxnState.COMMITTED)
    writer.effects = [
        Effect(txn_id="t-writer", index=0, tool="set_price", args={"sku": "sku1"},
               resource="sql", reversible=True, status=EffectStatus.APPLIED,
               write_keys=[["sql", ["prices", "sku1"], 5]],
               ts=datetime.now(timezone.utc)),
    ]
    consumer = Transaction(txn_id="t-consumer", state=TxnState.COMMITTED)
    consumer.effects = [
        Effect(txn_id="t-consumer", index=0, tool="read_price", args={"sku": "sku1"},
               resource="sql", reversible=True, status=EffectStatus.APPLIED,
               result={"price": 99}, read_keys=[["sql", ["prices", "sku1"], 5]],
               ts=datetime.now(timezone.utc)),
        Effect(txn_id="t-consumer", index=1, tool="set_margin", args={"sku": "sku1"},
               resource="sql", reversible=True, status=EffectStatus.APPLIED,
               write_keys=[["sql", ["margins", "sku1"], 1]],
               ts=datetime.now(timezone.utc)),
    ]
    path = str(tmp_path / "produced.db")
    j = AuditJournal(path)
    try:
        for t in (writer, consumer):
            j.record_transaction(t)
            for e in t.effects:
                j.record_effect(e)
    finally:
        j.close()

    with JournalReader(path) as r:
        tl = r.get_timeline("t-consumer")
        margin = next(e for e in tl["effects"] if e["tool"] == "set_margin")
        info = next(i for i in margin["informed_by"] if i["tool"] == "read_price")
        # the read of prices/sku1 v5 is grounded to the earlier txn's write
        assert info["produced_by"] == "t-writer#0"
        assert info["produced_by_external"] is False
        assert info["version"] == 5


def test_timeline_informed_by_tolerates_empty_read_keys(tmp_path: Path):
    """An effect with no reads (empty read_keys — the on-disk default) yields an
    empty informed_by, never a crash — the additive surface degrades cleanly on
    a journal whose effects recorded no read-set."""
    path = str(tmp_path / "noreads.db")
    AuditJournal(path).close()
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES ('t1', 'COMMITTED', ?, ?, 0)",
        (datetime.now(timezone.utc).isoformat(),) * 2,
    )
    # read_keys at its schema default '[]' — an effect that recorded no read-set
    con.execute(
        "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, reversible, "
        "status, args, read_keys, write_keys, ts) "
        "VALUES ('t1', 0, 'e', 'w', 'sql', 1, 'APPLIED', '{}', '[]', '[]', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    con.commit()
    con.close()
    with JournalReader(path) as r:
        tl = r.get_timeline("t1")
        assert tl["effects"][0]["informed_by"] == []


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
