"""Prong #2 (A) — conflict recording at the journal layer.

A commit-time isolation conflict used to live only as a raised
``IsolationConflict``; the journal went silent on it. These pin that
:meth:`AuditJournal.record_conflicts` / :meth:`get_conflicts` make a conflict
a first-class, append-only journal record — same shape as the verdict table —
and that the reader degrades cleanly on a journal that predates the table.

Offline: a seeded SQLite journal, no agent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pherix.core.audit import AuditJournal
from pherix.core.isolation import Conflict
from pherix.inspector.reader import JournalReader


def test_conflicts_table_exists_in_fresh_schema(tmp_path: Path):
    path = str(tmp_path / "j.db")
    AuditJournal(path).close()
    con = sqlite3.connect(path)
    tables = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    con.close()
    assert "conflicts" in tables


def test_record_and_get_conflicts_roundtrip(tmp_path: Path):
    path = str(tmp_path / "j.db")
    j = AuditJournal(path)
    j.record_conflicts(
        "txn-x",
        [
            Conflict(
                resource="sql",
                key=("releases", "current"),
                version_at_read=11,
                version_now=13,
                version_expected=11,
            ),
            Conflict(
                resource="fs",
                key=("deploy/manifest.json",),
                version_at_read="A",
                version_now="B",
                version_expected="A",
            ),
        ],
    )
    j.close()

    with JournalReader(path) as r:
        rows = r.get_conflicts("txn-x")
    assert len(rows) == 2
    first = rows[0]
    assert first["resource"] == "sql"
    assert first["key"] == ["releases", "current"]  # JSON tuple → list
    assert first["version_at_read"] == 11
    assert first["version_now"] == 13
    assert first["version_expected"] == 11
    assert first["seq"] == 0
    assert rows[1]["seq"] == 1
    assert rows[1]["version_now"] == "B"


def test_get_conflicts_empty_for_unconflicted_txn(tmp_path: Path):
    path = str(tmp_path / "j.db")
    AuditJournal(path).close()
    with JournalReader(path) as r:
        assert r.get_conflicts("txn-never-conflicted") == []


def test_reader_degrades_when_conflicts_table_absent(tmp_path: Path):
    """A journal written before conflict recording has no ``conflicts`` table.
    The reader must report zero / empty rather than raising — the NULL-tolerant
    degradation the spec calls for.

    This fails against the prior commit: the old reader has no ``get_conflicts``
    and ``stats()`` carries no ``conflict_total`` key.
    """
    path = str(tmp_path / "old.db")
    # Build a journal with ONLY the pre-Prong-#2 tables.
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
        assert r._has_conflicts is False
        assert r.get_conflicts("anything") == []
        assert r.stats()["conflict_total"] == 0
