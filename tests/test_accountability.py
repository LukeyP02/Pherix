"""Wedge #1 — JournalReader.accountability(), the per-actor governance ledger.

The reliability fold (Prong #2) answers *how often the system commits/fails*;
the lineage fold (Prong #3) answers *what read informed what write*. Neither
answers the action-governance question: **on whose authority did each action
happen, and how did that principal's actions land?** This fold does — a census
of every effect grouped by its recorded ``actor``.

The tests pin a hand-built multi-actor journal (written through the real
``Effect`` / ``AuditJournal`` so the rows are byte-for-byte what the engine
writes — the same approach the inspector seed uses) so a drift in either the
fold or the schema fails loudly: the per-actor reversible/irreversible split,
the ``irreversible_applied`` blast figure (fired-and-un-undoable only — a GATED
irreversible does NOT count), the ranking, the unattributed bucket, the dry-run
scope, the whole-scope totals, and NULL-tolerant degradation on a journal that
predates the ``actor`` column (``supported = false``, everything unattributed).

Offline: a hand-seeded SQLite journal and a localhost server.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState
from pherix.inspector.reader import ACCOUNTABILITY_CAVEAT, JournalReader
from pherix.inspector.seed import seed_demo_journal
from pherix.inspector.server import make_server


# --- a hand-built multi-actor journal ---------------------------------------
#
# Five transactions exercising every governance dimension:
#
#   txn-a1   COMMITTED   alice      read_db APPLIED(rev), charge_card APPLIED(irrev),
#                                   send_email GATED(irrev)
#   txn-a2   ROLLED_BACK alice      write_file COMPENSATED(rev)
#   txn-adm1 STUCK       role:admin debit COMPENSATED(rev), payout APPLIED(irrev),
#                                   notify FAILED(irrev)
#   txn-anon COMMITTED   (none)     query APPLIED(rev)            -> unattributed
#   txn-dry  COMMITTED*  alice      charge_card STAGED(irrev)     -> dry-run, excluded
#
# (* dry_run=1)


def _eff(
    txn_id: str,
    idx: int,
    tool: str,
    resource: str,
    reversible: bool,
    status: EffectStatus,
    actor: str | None,
) -> Effect:
    return Effect(
        txn_id=txn_id,
        index=idx,
        tool=tool,
        args={"i": idx},
        resource=resource,
        reversible=reversible,
        status=status,
        actor=actor,
        ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _write(journal: AuditJournal, txn: Transaction, **meta) -> None:
    journal.record_transaction(txn, **meta)
    for eff in txn.effects:
        journal.record_effect(eff)


def _seed_accountability(path: str) -> None:
    journal = AuditJournal(path)
    try:
        a1 = Transaction(txn_id="txn-a1", state=TxnState.COMMITTED)
        a1.effects = [
            _eff("txn-a1", 0, "read_db", "sql", True, EffectStatus.APPLIED, "alice"),
            _eff("txn-a1", 1, "charge_card", "http", False, EffectStatus.APPLIED, "alice"),
            _eff("txn-a1", 2, "send_email", "http", False, EffectStatus.GATED, "alice"),
        ]
        a2 = Transaction(txn_id="txn-a2", state=TxnState.ROLLED_BACK)
        a2.effects = [
            _eff("txn-a2", 0, "write_file", "fs", True, EffectStatus.COMPENSATED, "alice"),
        ]
        adm1 = Transaction(txn_id="txn-adm1", state=TxnState.STUCK)
        adm1.effects = [
            _eff("txn-adm1", 0, "debit", "sql", True, EffectStatus.COMPENSATED, "role:admin"),
            _eff("txn-adm1", 1, "payout", "http", False, EffectStatus.APPLIED, "role:admin"),
            _eff("txn-adm1", 2, "notify", "http", False, EffectStatus.FAILED, "role:admin"),
        ]
        anon = Transaction(txn_id="txn-anon", state=TxnState.COMMITTED)
        anon.effects = [
            _eff("txn-anon", 0, "query", "sql", True, EffectStatus.APPLIED, None),
        ]
        dry = Transaction(txn_id="txn-dry", state=TxnState.COMMITTED)
        dry.effects = [
            _eff("txn-dry", 0, "charge_card", "http", False, EffectStatus.STAGED, "alice"),
        ]
        _write(journal, a1)
        _write(journal, a2)
        _write(journal, adm1)
        _write(journal, anon)
        _write(journal, dry, dry_run=True)
    finally:
        journal.close()


@pytest.fixture
def reader(tmp_path: Path):
    path = str(tmp_path / "gov.db")
    _seed_accountability(path)
    r = JournalReader(path)
    yield r
    r.close()


def _by_actor(payload: dict) -> dict[str, dict]:
    return {a["actor"]: a for a in payload["actors"]}


# --- the named-actor records (dry-run excluded by default) ------------------


def test_alice_record_pins_every_dimension(reader: JournalReader):
    alice = _by_actor(reader.accountability())["alice"]
    assert alice["effects"] == 4                       # txn-a1 (3) + txn-a2 (1)
    assert alice["txns"] == 2
    assert alice["tools"] == [
        "charge_card", "read_db", "send_email", "write_file",
    ]
    assert alice["reversibility"] == {"reversible": 2, "irreversible": 2}
    assert alice["by_status"] == {
        "STAGED": 0, "APPLIED": 2, "COMPENSATED": 1, "GATED": 1, "FAILED": 0,
    }
    # charge_card fired (APPLIED + irreversible); send_email is irreversible but
    # GATED — held, never fired — so it is NOT counted as un-undoable blast.
    assert alice["irreversible_applied"] == 1
    assert alice["gated"] == 1
    assert alice["failed"] == 0
    assert alice["compensated"] == 1


def test_admin_record_pins_every_dimension(reader: JournalReader):
    admin = _by_actor(reader.accountability())["role:admin"]
    assert admin["effects"] == 3
    assert admin["txns"] == 1
    assert admin["tools"] == ["debit", "notify", "payout"]
    assert admin["reversibility"] == {"reversible": 1, "irreversible": 2}
    assert admin["by_status"] == {
        "STAGED": 0, "APPLIED": 1, "COMPENSATED": 1, "GATED": 0, "FAILED": 1,
    }
    assert admin["irreversible_applied"] == 1           # payout fired; notify FAILED
    assert admin["gated"] == 0
    assert admin["failed"] == 1
    assert admin["compensated"] == 1


def test_gated_irreversible_is_not_counted_as_blast(reader: JournalReader):
    """The blast figure is fired-and-un-undoable only. alice's send_email is an
    irreversible held at the gate — it never fired, so irreversible_applied
    stays at 1 (the charge), not 2. A plain ``status != APPLIED`` miss here
    would over-count the blast and is exactly what this pins against."""
    alice = _by_actor(reader.accountability())["alice"]
    assert alice["reversibility"]["irreversible"] == 2  # two irreversibles…
    assert alice["irreversible_applied"] == 1           # …but only one fired


# --- ranking ----------------------------------------------------------------


def test_actors_ranked_blast_then_volume_then_name(reader: JournalReader):
    """Both principals drove exactly one fired irreversible, so the tie breaks on
    effect volume: alice (4) outranks role:admin (3)."""
    names = [a["actor"] for a in reader.accountability()["actors"]]
    assert names == ["alice", "role:admin"]


# --- unattributed bucket ----------------------------------------------------


def test_unattributed_effects_are_surfaced_not_dropped(reader: JournalReader):
    un = reader.accountability()["unattributed"]
    assert un is not None
    assert un["actor"] is None
    assert un["effects"] == 1
    assert un["txns"] == 1
    assert un["tools"] == ["query"]
    assert un["irreversible_applied"] == 0
    # …and it is NOT also listed among the named principals.
    assert None not in _by_actor(reader.accountability())


# --- whole-scope totals -----------------------------------------------------


def test_totals_roll_up_named_plus_unattributed(reader: JournalReader):
    totals = reader.accountability()["totals"]
    assert totals["actors"] == 2                        # named principals only
    assert totals["effects"] == 8                       # 4 + 3 + 1 unattributed
    assert totals["irreversible_applied"] == 2          # charge + payout
    assert totals["gated"] == 1
    assert totals["failed"] == 1
    assert totals["compensated"] == 2
    assert totals["unattributed_effects"] == 1


# --- scope: dry-run excluded by default, re-includable ----------------------


def test_scope_default_excludes_dry_run(reader: JournalReader):
    payload = reader.accountability()
    assert payload["scope"]["include_dry_run"] is False
    assert payload["supported"] is True
    # alice's dry-run charge_card (STAGED) is NOT counted by default.
    alice = _by_actor(payload)["alice"]
    assert alice["effects"] == 4
    assert alice["by_status"]["STAGED"] == 0


def test_include_dry_run_adds_the_staged_charge(reader: JournalReader):
    payload = reader.accountability(include_dry_run=True)
    assert payload["scope"]["include_dry_run"] is True
    alice = _by_actor(payload)["alice"]
    assert alice["effects"] == 5                        # the dry-run effect joins
    assert alice["txns"] == 3
    assert alice["by_status"]["STAGED"] == 1
    assert alice["reversibility"]["irreversible"] == 3
    # …but a STAGED effect never fired, so the blast figure is unmoved.
    assert alice["irreversible_applied"] == 1
    assert payload["totals"]["effects"] == 9


def test_caveat_travels_with_the_payload(reader: JournalReader):
    assert reader.accountability()["caveat"] == ACCOUNTABILITY_CAVEAT
    assert "attribution, not authentication" in ACCOUNTABILITY_CAVEAT.lower()


# --- degradation: empty journal --------------------------------------------


def test_empty_journal_is_all_zeros_not_a_crash(tmp_path: Path):
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()
    with JournalReader(path) as r:
        payload = r.accountability()
        assert payload["actors"] == []
        assert payload["unattributed"] is None
        assert payload["supported"] is True            # column exists, just no rows
        assert payload["totals"]["effects"] == 0


# --- degradation: the seed has no actors → everything unattributed ----------


def test_seed_journal_folds_entirely_into_unattributed(tmp_path: Path):
    """The shared demo seed declares no ``actor`` on any effect, so the ledger
    has no named principals and the whole journal lands in ``unattributed`` —
    the honest reading of an attribution-free journal whose column nonetheless
    exists."""
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        payload = r.accountability()
        assert payload["supported"] is True
        assert payload["actors"] == []
        assert payload["unattributed"] is not None
        # Every counted (non-dry-run) effect is unattributed.
        assert payload["totals"]["unattributed_effects"] == payload["totals"]["effects"]


# --- degradation: NULL-tolerance on a pre-actor journal ---------------------

# The exact pre-actor ``effects`` schema (no ``actor`` column), mirroring
# tests/test_actor.py — a journal written before the field landed must fold
# cleanly into all-unattributed with ``supported = false``, never raising on the
# absent column.
_PRE_ACTOR_SCHEMA = """
CREATE TABLE transactions (
    txn_id        TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    replayed_from TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    client_id     TEXT
);
CREATE TABLE effects (
    txn_id     TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    effect_id  TEXT NOT NULL,
    tool       TEXT NOT NULL,
    resource   TEXT NOT NULL,
    reversible INTEGER NOT NULL,
    status     TEXT NOT NULL,
    args       TEXT NOT NULL,
    snapshot   TEXT,
    result     TEXT,
    read_keys  TEXT NOT NULL DEFAULT '[]',
    write_keys TEXT NOT NULL DEFAULT '[]',
    ts         TEXT NOT NULL,
    PRIMARY KEY (txn_id, idx)
);
"""


def _write_pre_actor_journal(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(_PRE_ACTOR_SCHEMA)
    c.execute(
        "INSERT INTO transactions "
        "(txn_id, state, created_at, updated_at, replayed_from, dry_run, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("txn-old", "COMMITTED", "2025-01-01T00:00:00+00:00",
         "2025-01-01T00:00:00+00:00", None, 0, "legacy-client"),
    )
    c.execute(
        "INSERT INTO effects "
        "(txn_id, idx, effect_id, tool, resource, reversible, status, args, "
        "snapshot, result, read_keys, write_keys, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("txn-old", 0, "eid0", "insert_widget", "sql", 0, "APPLIED",
         "{}", None, None, "[]", "[]", "2025-01-01T00:00:00+00:00"),
    )
    c.commit()
    c.close()


def test_pre_actor_journal_degrades_to_unattributed(tmp_path: Path):
    """Failing-before guard: without the ``NULL AS actor`` fallback for a journal
    whose effects table has no ``actor`` column, the fold's ``SELECT e.actor``
    raises OperationalError. The graceful degrade is the thing under test."""
    path = str(tmp_path / "legacy.db")
    _write_pre_actor_journal(path)
    with JournalReader(path) as r:
        payload = r.accountability()
        assert payload["supported"] is False
        assert payload["actors"] == []                 # no column → no named actors
        assert payload["unattributed"] is not None
        assert payload["unattributed"]["effects"] == 1
        # The one effect was an irreversible APPLIED → it counts as blast.
        assert payload["unattributed"]["irreversible_applied"] == 1
        assert payload["totals"]["unattributed_effects"] == 1


# --- the HTTP surface -------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path):
    db = str(tmp_path / "gov.db")
    _seed_accountability(db)
    httpd = make_server(db, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.reader.close()
        httpd.server_close()


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert "application/json" in resp.headers.get("Content-Type", "")
        return json.loads(resp.read())


def test_endpoint_serves_the_ledger(server: str):
    payload = _get_json(server + "/api/accountability")
    assert payload["supported"] is True
    assert [a["actor"] for a in payload["actors"]] == ["alice", "role:admin"]
    assert payload["totals"]["irreversible_applied"] == 2
    assert payload["unattributed"]["effects"] == 1


def test_endpoint_include_dry_run_param_flips_scope(server: str):
    base = _get_json(server + "/api/accountability")
    inc = _get_json(server + "/api/accountability?include_dry_run=1")
    assert base["scope"]["include_dry_run"] is False
    assert inc["scope"]["include_dry_run"] is True
    assert inc["totals"]["effects"] == base["totals"]["effects"] + 1
