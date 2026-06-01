"""Journal shipping — forward-read past a durable high-water cursor.

The control plane ingests the journal by reading it *forward* past a cursor:
``export_since(cursor)`` hands back every row appended since the mark and the
new mark to persist. It is the same forward fold the rest of the engine is built
on, truncated to "what I have not sent yet". These tests pin that surface
(``export_since`` + the durable ``get/set_ship_cursor``), which had no coverage
after the slim removed the sync-shipper suite.

Properties under test:
  * a fresh export (cursor=None) returns the whole journal across the three
    shippable tables, in dependency order, and a cursor at the high-water marks;
  * re-exporting at that cursor is empty (nothing new); appending a txn yields
    *only* the new rows (the forward read past the mark);
  * the durable cursor round-trips and its UPSERT is idempotent.
"""

from __future__ import annotations

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction

pytestmark = pytest.mark.audit


def _effect(txn_id: str, index: int, tool: str = "insert_user") -> Effect:
    return Effect(
        txn_id=txn_id,
        index=index,
        tool=tool,
        args={"name": "bob"},
        resource="sql",
        reversible=True,
        status=EffectStatus.APPLIED,
    )


def _seed_txn(j: AuditJournal, *, with_verdict: bool = True) -> str:
    """Record one full txn: a transaction row, an effect, and a verdict."""
    txn = Transaction()
    j.record_transaction(txn)
    j.record_effect(_effect(txn.txn_id, 0))
    if with_verdict:
        j.record_verdicts(
            txn.txn_id,
            [{"effect_index": 0, "phase": "commit", "allow": True,
              "kind": "rule", "rule_name": "allow_all", "reason": "ok"}],
        )
    return txn.txn_id


def test_export_since_none_returns_whole_journal_in_dependency_order():
    j = AuditJournal.in_memory()
    _seed_txn(j)

    rows_by_table, cursor = j.export_since(None)

    # All shippable tables present, in dependency order (a txn before its
    # effects, effects before verdicts / conflicts) — a control plane
    # validating FKs never sees an orphan. ``conflicts`` (Prong #2) is the
    # newest journal record and ships alongside the rest; this txn recorded
    # none, so its list is empty but the table is still present.
    assert list(rows_by_table.keys()) == [
        "transactions",
        "effects",
        "verdicts",
        "conflicts",
    ]
    assert len(rows_by_table["transactions"]) == 1
    assert len(rows_by_table["effects"]) == 1
    assert len(rows_by_table["verdicts"]) == 1
    assert rows_by_table["conflicts"] == []  # none recorded for this txn
    # Every row carries its rowid (the cursor coordinate).
    assert all("rowid" in r for rows in rows_by_table.values() for r in rows)
    # The cursor sits at the high-water rowid of each table (conflicts has no
    # rows, so it is absent from the cursor — only advanced tables appear).
    assert cursor == {"transactions": 1, "effects": 1, "verdicts": 1}


def test_export_since_is_a_forward_read_past_the_cursor():
    j = AuditJournal.in_memory()
    _seed_txn(j)
    _, high_water = j.export_since(None)

    # Re-reading at the high-water mark yields nothing new...
    rows_by_table, same_cursor = j.export_since(high_water)
    assert all(rows == [] for rows in rows_by_table.values())
    assert same_cursor == high_water

    # ...then a second txn appears, and only *its* rows come back.
    second = _seed_txn(j)
    new_rows, advanced = j.export_since(high_water)
    assert [r["txn_id"] for r in new_rows["transactions"]] == [second]
    assert len(new_rows["effects"]) == 1
    assert len(new_rows["verdicts"]) == 1
    # The cursor advanced past the new rows on every table.
    assert advanced["transactions"] == high_water["transactions"] + 1


def test_ship_cursor_round_trips_and_upsert_is_idempotent():
    j = AuditJournal.in_memory()

    # Empty before anything is persisted.
    assert j.get_ship_cursor() == {}

    j.set_ship_cursor({"transactions": 3, "effects": 5, "verdicts": 2})
    assert j.get_ship_cursor() == {"transactions": 3, "effects": 5, "verdicts": 2}

    # Advancing the same tables UPSERTs in place — no duplicate rows, new values.
    j.set_ship_cursor({"transactions": 9, "effects": 5, "verdicts": 7})
    assert j.get_ship_cursor() == {"transactions": 9, "effects": 5, "verdicts": 7}


def test_export_then_persist_cursor_resumes_where_it_left_off():
    """The end-to-end ship loop: export, persist the mark, restart, resume."""
    j = AuditJournal.in_memory()
    _seed_txn(j)

    _, cursor = j.export_since(None)
    j.set_ship_cursor(cursor)

    # A fresh export driven by the *durable* cursor sees nothing — all shipped.
    resumed, _ = j.export_since(j.get_ship_cursor())
    assert all(rows == [] for rows in resumed.values())
