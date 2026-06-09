"""Prong #2 — JournalReader.contention(), the isolation collision map.

reliability() surfaces a single ``conflict_total``; get_conflicts() lists one
transaction's conflicts. Neither answers the isolation-pillar / Wedge #2
question this fold does: **where do concurrent agents collide, and who loses
the race?** Every count is a *lost read* — the losing side of a recorded
non-commutative race — folded three ways: per ``(resource, key)`` hotspot, per
resource, and per agent (``client_id``).

The tests pin a hand-built journal of recorded conflicts (written through the
real ``AuditJournal.record_conflicts`` so the rows are byte-for-byte what the
engine writes — the same approach the accountability/reliability suites use) so
a drift in either the fold or the conflicts schema fails loudly: the hotspot
ranking, the per-resource rollup, the per-agent attribution, the
unattributed (null-client) bucket that keeps the counts reconciling, plus the
empty-journal zero and NULL-tolerant degradation on a journal that predates the
``conflicts`` table.

Offline: a hand-seeded SQLite journal and a localhost server.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.isolation import Conflict
from pherix.core.transaction import Transaction, TxnState
from pherix.inspector.reader import JournalReader
from pherix.inspector.seed import seed_demo_journal
from pherix.inspector.server import make_server


# --- a hand-built journal of recorded conflicts -----------------------------
#
# Five transactions across three agents race a handful of keys. Each conflict
# is a LOST read recorded against the losing transaction:
#
#   txn-q1  claude-code  sql/(users,7), sql/(orders,1)
#   txn-q2  claude-code  sql/(users,7)
#   txn-w1  aider        sql/(users,7)
#   txn-w2  aider        redis/(session,)
#   txn-anon (no client) redis/(session,)
#
# Totals fall out as: 6 conflicts; the sql/(users,7) row is the hotspot (3,
# fought by both named agents); sql is the hot resource (4 across 2 keys);
# claude-code loses the most reads (3); the unattributed txn keeps the
# per-client sum reconciling to the total.


def _conflict(resource: str, key: tuple) -> Conflict:
    # version_at_read != version_now is what makes it a conflict; the exact
    # versions don't matter to contention(), which counts collisions.
    return Conflict(
        resource=resource, key=key, version_at_read=1, version_now=2,
        version_expected=1,
    )


def _seed_contention(path: str) -> None:
    journal = AuditJournal(path)
    try:
        rows = [
            ("txn-q1", "claude-code", [_conflict("sql", ("users", 7)),
                                       _conflict("sql", ("orders", 1))]),
            ("txn-q2", "claude-code", [_conflict("sql", ("users", 7))]),
            ("txn-w1", "aider", [_conflict("sql", ("users", 7))]),
            ("txn-w2", "aider", [_conflict("redis", ("session",))]),
            ("txn-anon", None, [_conflict("redis", ("session",))]),
        ]
        for txn_id, client_id, conflicts in rows:
            journal.record_transaction(
                Transaction(txn_id=txn_id, state=TxnState.ROLLED_BACK),
                client_id=client_id,
            )
            journal.record_conflicts(txn_id, conflicts)
    finally:
        journal.close()


@pytest.fixture
def reader(tmp_path: Path):
    path = str(tmp_path / "contention.db")
    _seed_contention(path)
    r = JournalReader(path)
    yield r
    r.close()


def _empty_reader(tmp_path: Path) -> JournalReader:
    path = str(tmp_path / "empty.db")
    AuditJournal(path).close()  # creates the schema (incl. an empty conflicts table)
    return JournalReader(path)


# --- scope + total ----------------------------------------------------------


def test_total_counts_every_recorded_conflict(reader: JournalReader):
    """Fails against the prior commit: there was no contention() at all.

    Six lost reads were recorded across the five transactions; the headline
    total is the raw conflict count, regardless of each txn's final state."""
    con = reader.contention()
    assert con["scope"]["conflicts_recorded"] is True
    assert con["total"] == 6


# --- hotspots: the (resource, key) rows agents fight over -------------------


def test_hotspots_ranked_with_agents_and_txns(reader: JournalReader):
    hot = reader.contention()["hotspots"]
    # Ranked conflicts-desc, then resource, then key.
    assert [(h["resource"], h["key"], h["conflicts"]) for h in hot] == [
        ("sql", ["users", 7], 3),
        ("redis", ["session"], 2),
        ("sql", ["orders", 1], 1),
    ]
    top = hot[0]
    # The busiest key is fought by three distinct losing txns and BOTH agents.
    assert top["transactions"] == ["txn-q1", "txn-q2", "txn-w1"]
    assert top["clients"] == ["aider", "claude-code"]


def test_hotspot_null_client_is_excluded_from_named_agents(reader: JournalReader):
    """The redis/(session) row was lost once by aider and once by an
    unattributed txn — only the named agent appears in ``clients``, but both
    losses still count toward ``conflicts``."""
    hot = reader.contention()["hotspots"]
    redis = next(h for h in hot if h["resource"] == "redis")
    assert redis["conflicts"] == 2
    assert redis["transactions"] == ["txn-anon", "txn-w2"]
    assert redis["clients"] == ["aider"]  # the null-client txn names no agent


# --- resources: which backend is hot ----------------------------------------


def test_resources_rollup(reader: JournalReader):
    res = {r["resource"]: r for r in reader.contention()["resources"]}
    # sql carries 4 of the 6 conflicts across two distinct keys and three txns.
    assert res["sql"]["conflicts"] == 4
    assert res["sql"]["keys"] == 2
    assert res["sql"]["transactions"] == 3
    assert res["sql"]["clients"] == ["aider", "claude-code"]
    assert res["redis"]["conflicts"] == 2
    assert res["redis"]["keys"] == 1
    assert res["redis"]["transactions"] == 2
    assert res["redis"]["clients"] == ["aider"]


def test_resources_ranked_hot_first(reader: JournalReader):
    assert [r["resource"] for r in reader.contention()["resources"]] == [
        "sql", "redis",
    ]


# --- by_client: which agent is least isolation-safe -------------------------


def test_by_client_attribution_and_reconciliation(reader: JournalReader):
    con = reader.contention()
    by_client = {c["client_id"]: c for c in con["by_client"]}
    assert by_client["claude-code"]["conflicts"] == 3   # txn-q1 (2) + txn-q2 (1)
    assert by_client["claude-code"]["resources"] == 1    # sql only
    assert by_client["claude-code"]["keys"] == 2         # users:7 + orders:1
    assert by_client["aider"]["conflicts"] == 2          # txn-w1 + txn-w2
    assert by_client["aider"]["resources"] == 2          # sql + redis
    assert by_client["aider"]["keys"] == 2
    # The unattributed txn keeps its own bucket so the per-client counts sum
    # back to the headline total — nothing is silently dropped.
    assert by_client[None]["conflicts"] == 1
    assert sum(c["conflicts"] for c in con["by_client"]) == con["total"]


def test_by_client_ranked_loudest_first_null_last(reader: JournalReader):
    """Loudest loser first; the unattributed bucket sorts last among its
    tier so a real agent never hides behind the anonymous reads."""
    assert [c["client_id"] for c in reader.contention()["by_client"]] == [
        "claude-code", "aider", None,
    ]


# --- empty + NULL-tolerant degradation --------------------------------------


def test_empty_journal_is_zeroed_not_crashing(tmp_path: Path):
    """A journal with the conflicts table but no rows: present-and-zero, every
    collection empty — never a ZeroDivisionError or a missing key."""
    with _empty_reader(tmp_path) as r:
        con = r.contention()
    assert con["scope"]["conflicts_recorded"] is True
    assert con["total"] == 0
    assert con["hotspots"] == []
    assert con["resources"] == []
    assert con["by_client"] == []


def test_pre_conflicts_journal_degrades(tmp_path: Path):
    """A journal written before the conflicts table existed must still yield a
    full (empty) contention payload rather than crashing — the same
    NULL-tolerant degradation reliability() guarantees. ``conflicts_recorded``
    is False so a console can say "this journal predates conflict recording"
    rather than "no conflicts seen"."""
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
    con.commit()
    con.close()

    with JournalReader(path) as r:
        payload = r.contention()
    assert payload == {
        "scope": {"conflicts_recorded": False},
        "total": 0,
        "hotspots": [],
        "resources": [],
        "by_client": [],
    }


# --- the seeded demo journal (no conflicts) keeps a clean, empty map --------


def test_seed_journal_has_no_contention(tmp_path: Path):
    """The shipped demo journal records no conflicts, so contention() is the
    structured-empty shape — pinned so a future seed that adds conflicts
    updates this deliberately rather than by accident."""
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    with JournalReader(path) as r:
        con = r.contention()
    assert con["scope"]["conflicts_recorded"] is True
    assert con["total"] == 0
    assert con["hotspots"] == []


# --- over the wire ----------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path):
    db = str(tmp_path / "contention.db")
    _seed_contention(db)
    httpd = make_server(db, host="127.0.0.1", port=0)  # 0 → OS picks a free port
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
        return resp.status, json.loads(resp.read())


def test_api_contention_round_trip(server: str):
    status, data = _get_json(server + "/api/contention")
    assert status == 200
    assert set(data) >= {"scope", "total", "hotspots", "resources", "by_client"}
    assert data["total"] == 6
    # the headline hotspot survives the round trip, agents and all
    top = data["hotspots"][0]
    assert (top["resource"], top["key"], top["conflicts"]) == ("sql", ["users", 7], 3)
    assert top["clients"] == ["aider", "claude-code"]
    assert [c["client_id"] for c in data["by_client"]] == ["claude-code", "aider", None]
