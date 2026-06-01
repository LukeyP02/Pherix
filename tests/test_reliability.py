"""Prong #2 (B) — JournalReader.reliability(), a GROUP-BY over the journal.

Pins exact rates against the seeded demo journal so a drift in either the
seeder or the metric fails loudly: the settled denominator, the
failed-before-gated tool ranking, dry-run in/exclusion, the empty-journal
zero-rate guard, and NULL-tolerant degradation on a journal that predates the
verdicts / conflicts tables.

The seeded journal (excluding the one dry-run, ``txn-dryrun-plan05``) holds
six transactions:

  * txn-clean-deploy01  COMMITTED   3 effects, all APPLIED
  * txn-rollback-rel02  ROLLED_BACK 2 effects, both COMPENSATED
  * txn-gated-charge03  STAGED      read_invoice APPLIED, charge_card GATED
  * txn-stuck-payout04  STUCK       debit COMPENSATED, payout APPLIED, notify FAILED
  * txn-clientA-q06     COMMITTED   1 effect APPLIED
  * txn-clientB-w07     COMMITTED   1 effect APPLIED

Settled (terminal) txns: COMMITTED 3, ROLLED_BACK 1, STUCK 1 → 5. The STAGED
gated txn is in-flight, not settled. Offline: a seeded SQLite journal.
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


def _empty_reader(tmp_path: Path) -> JournalReader:
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()
    return JournalReader(path)


# --- scope block ------------------------------------------------------------


def test_scope_states_dry_run_exclusion_and_denials_scope(reader: JournalReader):
    rel = reader.reliability()
    assert rel["scope"]["include_dry_run"] is False  # excluded by default
    assert rel["scope"]["denials_scope"] == "all_verdicts"
    assert rel["scope"]["settled_states"] == [
        "COMMITTED",
        "ROLLED_BACK",
        "PARTIAL",
        "STUCK",
    ]


# --- transaction outcome rates over the settled denominator -----------------


def test_outcome_rates_over_settled_denominator(reader: JournalReader):
    rel = reader.reliability()
    out = rel["outcomes"]
    # Settled = COMMITTED 3 + ROLLED_BACK 1 + STUCK 1 = 5. The STAGED gated
    # txn is in-flight and excluded from the denominator.
    assert out["settled"] == 5
    assert out["counts"] == {
        "COMMITTED": 3,
        "ROLLED_BACK": 1,
        "PARTIAL": 0,
        "STUCK": 1,
    }
    assert out["rates"]["commit"] == pytest.approx(3 / 5)
    assert out["rates"]["rollback"] == pytest.approx(1 / 5)
    assert out["rates"]["partial"] == pytest.approx(0.0)
    assert out["rates"]["stuck"] == pytest.approx(1 / 5)


def test_staged_txn_is_not_in_settled_denominator(reader: JournalReader):
    """The gated STAGED txn must not inflate the denominator — settled is 5,
    not 6. Rates would all shift if an in-flight txn were counted."""
    rel = reader.reliability()
    assert rel["outcomes"]["settled"] == 5
    # If STAGED were (wrongly) counted, commit rate would be 3/6 = 0.5.
    assert rel["outcomes"]["rates"]["commit"] != pytest.approx(0.5)


# --- effect outcome rates + gate incidence ----------------------------------


def test_effect_rates_over_non_dry_run_effects(reader: JournalReader):
    rel = reader.reliability()
    eff = rel["effects"]
    # 12 effects across the 6 non-dry-run txns: 7 APPLIED, 3 COMPENSATED,
    # 1 GATED, 1 FAILED.
    assert eff["total"] == 12
    assert eff["counts"]["APPLIED"] == 7
    assert eff["counts"]["COMPENSATED"] == 3
    assert eff["counts"]["GATED"] == 1
    assert eff["counts"]["FAILED"] == 1
    assert eff["rates"]["gate"] == pytest.approx(1 / 12)
    assert eff["rates"]["failure"] == pytest.approx(1 / 12)
    assert eff["rates"]["compensated"] == pytest.approx(3 / 12)


def test_gate_incidence_is_txn_level(reader: JournalReader):
    rel = reader.reliability()
    # Exactly one of the 6 non-dry-run txns (gated-charge03) carries a gate.
    assert rel["effects"]["gate_incidence"] == pytest.approx(1 / 6)


# --- top-failing tools: FAILED / GATED, never COMPENSATED -------------------


def test_top_failing_tools_failed_before_gated(reader: JournalReader):
    """notify_vendor (1 FAILED) and charge_card (1 GATED) tie on total; the
    failed-before-gated tiebreak ranks the hard failure first."""
    rel = reader.reliability()
    tools = rel["top_failing_tools"]
    names = [t["tool"] for t in tools]
    assert names == ["notify_vendor", "charge_card"]
    notify = tools[0]
    assert notify == {"tool": "notify_vendor", "failed": 1, "gated": 0, "total": 1}
    charge = tools[1]
    assert charge == {"tool": "charge_card", "failed": 0, "gated": 1, "total": 1}


def test_compensated_tools_never_appear_in_failing_list(reader: JournalReader):
    """A COMPENSATED effect SUCCEEDED then was cleanly undone — that is the
    system working, not a tool failing. debit_ledger / bump_version /
    write_manifest are COMPENSATED in the seed and must not be listed."""
    rel = reader.reliability()
    names = {t["tool"] for t in rel["top_failing_tools"]}
    assert "debit_ledger" not in names
    assert "bump_version" not in names
    assert "write_manifest" not in names


# --- denial-reason rollup (all verdicts, dry-run included) ------------------


def test_denials_span_all_verdicts_including_dry_run(reader: JournalReader):
    """The denial rollup is the one section that counts dry-run verdicts: the
    dry-run's two budget-guard denials and the gated txn's one cap denial."""
    rel = reader.reliability()
    by_reason = {d["reason"]: d["count"] for d in rel["denials"]}
    # The dry-run denies the same rule at stage AND commit → 2 rows.
    assert by_reason["charge 2000 exceeds remaining budget 1500"] == 2
    # The gated txn's commit-time cap denial → 1 row.
    cap_reason = next(r for r in by_reason if "5000" in r)
    assert by_reason[cap_reason] == 1
    # Commonest first.
    assert rel["denials"][0]["count"] == 2


def test_denials_unaffected_by_include_dry_run_flag(reader: JournalReader):
    """The denial rollup is all-verdicts scope regardless of include_dry_run —
    the dry-run's denials show up either way."""
    excl = reader.reliability(include_dry_run=False)["denials"]
    incl = reader.reliability(include_dry_run=True)["denials"]
    assert excl == incl


# --- held-back staged/gated chains ------------------------------------------


def test_held_back_lists_the_gated_staged_txn(reader: JournalReader):
    rel = reader.reliability()
    assert rel["held_back"] == [
        {"txn_id": "txn-gated-charge03", "state": "STAGED"}
    ]


# --- dry-run inclusion flips the txn-scoped sections ------------------------


def test_include_dry_run_changes_effect_total_but_not_denials(reader: JournalReader):
    excl = reader.reliability(include_dry_run=False)
    incl = reader.reliability(include_dry_run=True)
    # The dry-run adds 2 effects (read_budget APPLIED + charge_card STAGED).
    assert incl["effects"]["total"] == excl["effects"]["total"] + 2
    assert incl["effects"]["counts"]["STAGED"] == 1  # the dry-run's staged charge
    # The dry-run txn is COMMITTED, so including it bumps settled 5 → 6.
    assert incl["outcomes"]["settled"] == 6
    assert incl["scope"]["include_dry_run"] is True


# --- conflict_total surfaced in the payload ---------------------------------


def test_conflict_total_zero_on_seed(reader: JournalReader):
    # The seeded journal has no recorded conflicts (the seeder predates the
    # conflict story); the field is present and zero, not absent.
    assert reader.reliability()["conflict_total"] == 0


def test_conflict_total_reflects_recorded_conflicts(tmp_path: Path):
    """A journal with recorded conflicts surfaces the count in reliability()."""
    from pherix.core.isolation import Conflict

    path = str(tmp_path / "withconflicts.db")
    j = AuditJournal(path)
    j.record_conflicts(
        "txn-c",
        [Conflict(resource="sql", key=("k",), version_at_read=1, version_now=2)],
    )
    j.close()
    with JournalReader(path) as r:
        assert r.reliability()["conflict_total"] == 1


# --- empty-journal zero-rate guard ------------------------------------------


def test_empty_journal_zero_rates(tmp_path: Path):
    """No txns, no effects → every rate is 0.0 (not a ZeroDivisionError) and
    every collection is empty. The empty-journal guard."""
    with _empty_reader(tmp_path) as r:
        rel = r.reliability()
    assert rel["outcomes"]["settled"] == 0
    assert all(v == 0.0 for v in rel["outcomes"]["rates"].values())
    assert rel["effects"]["total"] == 0
    assert all(v == 0.0 for v in rel["effects"]["rates"].values())
    assert rel["effects"]["gate_incidence"] == 0.0
    assert rel["top_failing_tools"] == []
    assert rel["denials"] == []
    assert rel["held_back"] == []
    assert rel["conflict_total"] == 0


# --- NULL-tolerant degradation: pre-verdicts / pre-conflicts journal --------


def test_reliability_on_pre_verdicts_pre_conflicts_journal(tmp_path: Path):
    """A journal written before the verdicts AND conflicts tables existed
    must still yield a full reliability payload — empty denials, zero
    conflicts — rather than crashing. The NULL-tolerant degradation guard.

    Fails against the prior commit: there was no reliability() at all.
    """
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
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO transactions (txn_id, state, created_at, updated_at, dry_run) "
        "VALUES ('t1', 'COMMITTED', ?, ?, 0)",
        (now, now),
    )
    con.execute(
        "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, reversible, "
        "status, args, read_keys, write_keys, ts) "
        "VALUES ('t1', 0, 'e', 'w', 'sql', 1, 'FAILED', '{}', '[]', '[]', ?)",
        (now,),
    )
    con.commit()
    con.close()

    with JournalReader(path) as r:
        rel = r.reliability()
    assert rel["outcomes"]["settled"] == 1
    assert rel["outcomes"]["rates"]["commit"] == pytest.approx(1.0)
    assert rel["effects"]["total"] == 1
    assert rel["effects"]["rates"]["failure"] == pytest.approx(1.0)
    # The one FAILED effect is a failing tool.
    assert rel["top_failing_tools"] == [
        {"tool": "w", "failed": 1, "gated": 0, "total": 1}
    ]
    # No verdicts table → empty denials, no crash. No conflicts table → 0.
    assert rel["denials"] == []
    assert rel["conflict_total"] == 0
