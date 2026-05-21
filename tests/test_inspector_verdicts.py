"""Per-rule policy verdict persistence — the additive engine touch.

Two layers:
  1. The audit journal's ``record_verdicts`` / ``get_verdicts`` round-trip.
  2. The dry-run path writing the verdicts it already captures, end-to-end,
     such that the inspector's reader renders per-effect stage/commit
     decisions (including a Deny).

The normal-commit path is intentionally untouched (verdict capture there is
a clean follow-up); these pin the path that ships now.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pherix import Allow, AuditJournal, Deny, Policy, SQLiteAdapter, dry_run, tool
from pherix.inspector.reader import JournalReader


# --- 1. audit round-trip ----------------------------------------------------


def test_record_and_get_verdicts_roundtrip():
    audit = AuditJournal.in_memory()
    audit.record_verdicts("txn-1", [
        {"effect_index": 0, "phase": "stage", "allow": True, "kind": "rule",
         "rule_name": "r1", "reason": None},
        {"effect_index": 0, "phase": "commit", "allow": False, "kind": "cap",
         "rule_name": "Cap.sum(x)", "reason": "over cap"},
    ])
    rows = audit.get_verdicts("txn-1")
    assert len(rows) == 2
    # ordered by (effect_index, seq) — insertion order, stage before commit
    assert rows[0]["phase"] == "stage" and rows[0]["allow"] == 1
    assert rows[1]["phase"] == "commit" and rows[1]["allow"] == 0
    assert rows[1]["kind"] == "cap" and rows[1]["reason"] == "over cap"
    audit.close()


def test_get_verdicts_empty_for_unknown_txn():
    audit = AuditJournal.in_memory()
    assert audit.get_verdicts("nope") == []
    audit.close()


# --- 2. dry-run persists what it captures -----------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    yield c
    c.close()


def test_dry_run_persists_verdicts_and_reader_renders_them(conn, tmp_path: Path):
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    policy = Policy.allow_all()

    @policy.rule
    def no_secrets(effect, ctx):
        if "secret" in effect.args.get("body", ""):
            return Deny("body contains a secret")
        return Allow()

    db = str(tmp_path / "audit.db")
    audit = AuditJournal(db)
    with dry_run({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit) as ctx:
        insert_note(body="hello")
        insert_note(body="my secret token")
    txn_id = ctx.txn_id

    # Persisted: both phases (stage + commit) for both effects.
    rows = audit.get_verdicts(txn_id)
    assert rows, "dry-run should have persisted verdicts"
    phases = {r["phase"] for r in rows}
    assert phases == {"stage", "commit"}
    denies = [r for r in rows if not r["allow"]]
    assert denies and all(r["reason"] == "body contains a secret" for r in denies)
    audit.close()

    # The reader renders them per effect, attached to the offending effect.
    with JournalReader(db) as r:
        tl = r.get_timeline(txn_id)
        assert tl is not None
        denied_effect = next(
            e for e in tl["effects"]
            if any(not v["allow"] for v in e["policy_verdicts"])
        )
        assert denied_effect["args"]["body"] == "my secret token"
        assert r.stats()["has_verdicts"] is True
