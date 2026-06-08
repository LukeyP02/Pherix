"""The inspector's undo-impact fold — the blast radius of reversing a txn.

The product promises *undo*; ``undo_impact(txn_id)`` is the pre-flight check the
promise implies: before you reverse a committed transaction, what depends on it?
It is lineage's version-grounded ``produces`` relation read in reverse, folded
into an undo-safety verdict — a pure traversal of the journal's recorded
read/write keys and the resources' version counters, recomputing nothing from
the live world.

Two hazards drive the verdict, each pinned by a dedicated journal:

  * **downstream consumers** — a later transaction read the EXACT version this
    one wrote; undoing it retroactively invalidates that input. A *committed*
    consumer is a standing dependency; an in-flight one is provisional; a
    rolled-back one never stood.
  * **superseded keys** — a later LIVE (APPLIED) write overwrote one of this
    transaction's keys, so an undo would roll back from a value it did not write.

Plus the hard-floor irreversible case, the not-committed case, the honest-scope
caveat, the versionless filesystem blind spot, and the unknown-txn ``None``.
Fully offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState
from pherix.inspector.reader import UNDO_IMPACT_CAVEAT, JournalReader
from pherix.inspector.seed import seed_demo_journal


# --- helpers ----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_journal(path: str, txns: list[Transaction], meta: dict | None = None) -> None:
    """Persist each transaction and its effects through the real journal, so the
    rows are byte-for-byte what the engine would have written."""
    meta = meta or {}
    j = AuditJournal(path)
    try:
        for t in txns:
            j.record_transaction(t, **meta.get(t.txn_id, {}))
            for e in t.effects:
                j.record_effect(e)
    finally:
        j.close()


def _eff(txn_id, idx, tool, *, status, reversible=True, resource="sql",
         reads=None, writes=None, compensator=None) -> Effect:
    return Effect(
        txn_id=txn_id, index=idx, tool=tool, args={}, resource=resource,
        reversible=reversible, status=status, read_keys=reads or [],
        write_keys=writes or [], compensator=compensator, ts=_now(),
    )


# --- fixtures (seed) --------------------------------------------------------


@pytest.fixture
def demo_db(tmp_path: Path) -> str:
    path = str(tmp_path / "demo.db")
    seed_demo_journal(path)
    return path


# --- unknown / not-committed ------------------------------------------------


def test_unknown_transaction_returns_none(demo_db: str):
    """A txn id not in the journal yields None — the HTTP layer renders 404."""
    with JournalReader(demo_db) as r:
        assert r.undo_impact("txn-does-not-exist") is None


def test_not_committed_target_is_flagged(demo_db: str):
    """The seed's gated charge is STAGED, not COMMITTED — there is nothing
    committed to reverse, so the verdict says so rather than pretending."""
    with JournalReader(demo_db) as r:
        out = r.undo_impact("txn-gated-charge03")
    assert out["verdict"]["label"] == "not-committed"
    assert out["verdict"]["undoable"] is False
    assert out["verdict"]["clean"] is False
    assert out["target"]["is_committed"] is False
    assert "not COMMITTED" in out["verdict"]["reason"]


# --- clean undo (seed) ------------------------------------------------------


def test_seed_clean_deploy_is_a_self_contained_undo(demo_db: str):
    """Nobody in the seed read releases/current v12, and the only later write to
    that key (the rollback's v13) is COMPENSATED, not live — so undoing the clean
    deploy is self-contained. Its versionless fs write is surfaced as a blind
    spot, never silently dropped."""
    with JournalReader(demo_db) as r:
        out = r.undo_impact("txn-clean-deploy01")

    assert out["verdict"]["label"] == "clean"
    assert out["verdict"]["undoable"] is True
    assert out["verdict"]["clean"] is True
    assert out["downstream"] == []
    assert out["superseded"] == []
    # the live versioned write an undo would reverse
    assert {"resource": "sql", "key": ["releases", "current"], "version": 12,
            "by_idx": 1, "by_tool": "bump_version"} in out["target"]["writes"]
    # the filesystem manifest has no version → it can't anchor a downstream edge,
    # surfaced separately rather than pretended to be safe
    assert out["target"]["versionless_writes"] == [
        {"resource": "fs", "key": ["deploy/manifest.json"],
         "by_idx": 2, "by_tool": "write_manifest"}
    ]
    assert out["totals"]["downstream_transactions"] == 0


# --- entangled: a committed downstream consumer -----------------------------


def test_committed_consumer_makes_undo_entangled(tmp_path: Path):
    """t-writer commits prices/sku1 v5; t-reader commits a read of v5. The
    version ties them: undoing t-writer retroactively invalidates t-reader's
    input, so the verdict is 'entangled' and names the consumer."""
    writer = Transaction(txn_id="t-writer", state=TxnState.COMMITTED)
    writer.effects = [
        _eff("t-writer", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 5]]),
    ]
    reader_txn = Transaction(txn_id="t-reader", state=TxnState.COMMITTED)
    reader_txn.effects = [
        _eff("t-reader", 0, "read_price", status=EffectStatus.APPLIED,
             reads=[["sql", ["prices", "sku1"], 5]]),
    ]
    path = str(tmp_path / "consumer.db")
    _write_journal(path, [writer, reader_txn])

    with JournalReader(path) as r:
        out = r.undo_impact("t-writer")

    assert out["verdict"]["label"] == "entangled"
    assert out["verdict"]["undoable"] is True  # mechanically possible…
    assert out["verdict"]["clean"] is False    # …but not free
    assert out["totals"]["downstream_transactions"] == 1
    assert out["totals"]["committed_consumers"] == 1
    assert out["totals"]["downstream_reads"] == 1
    consumer = out["downstream"][0]
    assert consumer["txn_id"] == "t-reader"
    assert consumer["committed"] is True
    assert consumer["read_count"] == 1
    assert consumer["reads"][0]["version"] == 5
    assert "retroactively invalidates" in out["verdict"]["reason"]


def test_consumer_must_match_the_exact_version(tmp_path: Path):
    """A read of a DIFFERENT version is not a consumer of this write — the fold
    does not guess. v4 ≠ v5 ⇒ no downstream, undo stays clean."""
    writer = Transaction(txn_id="t-w", state=TxnState.COMMITTED)
    writer.effects = [
        _eff("t-w", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 5]]),
    ]
    other = Transaction(txn_id="t-o", state=TxnState.COMMITTED)
    other.effects = [
        _eff("t-o", 0, "read_price", status=EffectStatus.APPLIED,
             reads=[["sql", ["prices", "sku1"], 4]]),  # v4 ≠ v5
    ]
    path = str(tmp_path / "nomatch.db")
    _write_journal(path, [writer, other])

    with JournalReader(path) as r:
        out = r.undo_impact("t-w")
    assert out["downstream"] == []
    assert out["verdict"]["label"] == "clean"


def test_in_flight_consumer_is_listed_but_does_not_flip_verdict(tmp_path: Path):
    """An OPEN transaction reading v5 is a provisional reader: surfaced and
    counted, but the headline stays 'clean' (it has not committed), with a note
    telling the operator to re-check."""
    writer = Transaction(txn_id="t-writer", state=TxnState.COMMITTED)
    writer.effects = [
        _eff("t-writer", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 5]]),
    ]
    inflight = Transaction(txn_id="t-open", state=TxnState.OPEN)
    inflight.effects = [
        _eff("t-open", 0, "read_price", status=EffectStatus.APPLIED,
             reads=[["sql", ["prices", "sku1"], 5]]),
    ]
    path = str(tmp_path / "inflight.db")
    _write_journal(path, [writer, inflight])

    with JournalReader(path) as r:
        out = r.undo_impact("t-writer")

    assert out["verdict"]["label"] == "clean"
    assert out["totals"]["committed_consumers"] == 0
    assert out["totals"]["in_flight_consumers"] == 1
    assert out["downstream"][0]["in_flight"] is True
    assert "in-flight" in out["verdict"]["reason"]


def test_rolled_back_consumer_does_not_entangle(tmp_path: Path):
    """A read inside a ROLLED_BACK transaction never stood — it is surfaced but
    is neither committed nor in-flight, so the undo stays clean."""
    writer = Transaction(txn_id="t-writer", state=TxnState.COMMITTED)
    writer.effects = [
        _eff("t-writer", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 5]]),
    ]
    rolled = Transaction(txn_id="t-rb", state=TxnState.ROLLED_BACK)
    rolled.effects = [
        _eff("t-rb", 0, "read_price", status=EffectStatus.COMPENSATED,
             reads=[["sql", ["prices", "sku1"], 5]]),
    ]
    path = str(tmp_path / "rolledback.db")
    _write_journal(path, [writer, rolled])

    with JournalReader(path) as r:
        out = r.undo_impact("t-writer")

    assert out["verdict"]["label"] == "clean"
    assert out["totals"]["downstream_transactions"] == 1   # surfaced…
    assert out["totals"]["committed_consumers"] == 0       # …but not standing
    assert out["downstream"][0]["committed"] is False
    assert out["downstream"][0]["in_flight"] is False


# --- entangled: a superseding later write -----------------------------------


def test_superseded_key_makes_undo_entangled(tmp_path: Path):
    """t-old commits prices/sku1 v5; t-new commits a live v6 over the same key.
    Undoing t-old would restore from a value it did not write — flagged as a
    superseded key, naming the later writer."""
    old = Transaction(txn_id="t-old", state=TxnState.COMMITTED)
    old.effects = [
        _eff("t-old", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 5]]),
    ]
    new = Transaction(txn_id="t-new", state=TxnState.COMMITTED)
    new.effects = [
        _eff("t-new", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 6]]),
    ]
    path = str(tmp_path / "superseded.db")
    _write_journal(path, [old, new])

    with JournalReader(path) as r:
        out = r.undo_impact("t-old")

    assert out["verdict"]["label"] == "entangled"
    assert out["totals"]["superseded_keys"] == 1
    s = out["superseded"][0]
    assert s["resource"] == "sql"
    assert s["key"] == ["prices", "sku1"]
    assert s["my_version"] == 5
    assert s["latest_version"] == 6
    assert s["by_txn"] == "t-new"
    assert "did not write" in out["verdict"]["reason"]


def test_compensated_later_write_does_not_supersede(tmp_path: Path):
    """A later write that was itself COMPENSATED (rolled back) is not live, so it
    does not supersede this transaction's value — the undo stays clean."""
    old = Transaction(txn_id="t-old", state=TxnState.COMMITTED)
    old.effects = [
        _eff("t-old", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "sku1"], 5]]),
    ]
    undone = Transaction(txn_id="t-undone", state=TxnState.ROLLED_BACK)
    undone.effects = [
        _eff("t-undone", 0, "set_price", status=EffectStatus.COMPENSATED,
             writes=[["sql", ["prices", "sku1"], 6]]),
    ]
    path = str(tmp_path / "comp.db")
    _write_journal(path, [old, undone])

    with JournalReader(path) as r:
        out = r.undo_impact("t-old")
    assert out["superseded"] == []
    assert out["verdict"]["label"] == "clean"


# --- hard floor: an irreversible applied effect -----------------------------


def test_irreversible_applied_effect_blocks_auto_undo(tmp_path: Path):
    """A committed txn that fired an irreversible effect (no compensator) has no
    clean undo regardless of blast — the hard floor the whole product is built
    around. The irreversible verdict takes precedence over entanglement."""
    t = Transaction(txn_id="t-charge", state=TxnState.COMMITTED)
    t.effects = [
        _eff("t-charge", 0, "write_ledger", status=EffectStatus.APPLIED,
             writes=[["sql", ["ledger", "acct-1"], 9]]),
        _eff("t-charge", 1, "charge_card", status=EffectStatus.APPLIED,
             reversible=False, resource="http"),  # irreversible, fired
    ]
    path = str(tmp_path / "irreversible.db")
    _write_journal(path, [t])

    with JournalReader(path) as r:
        out = r.undo_impact("t-charge")

    assert out["verdict"]["label"] == "blocked-irreversible"
    assert out["verdict"]["undoable"] is False
    assert out["target"]["irreversible_applied"] == 1
    assert out["target"]["reversible_applied"] == 1
    assert "cannot be auto-undone" in out["verdict"]["reason"]


def test_irreversible_precedence_over_entanglement(tmp_path: Path):
    """Even with a committed downstream consumer, an irreversible fired effect is
    the headline: you can't auto-undo it at all, so 'blocked-irreversible' wins
    over 'entangled' (but the blast is still surfaced for the operator)."""
    t = Transaction(txn_id="t-mix", state=TxnState.COMMITTED)
    t.effects = [
        _eff("t-mix", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["prices", "x"], 3]]),
        _eff("t-mix", 1, "charge_card", status=EffectStatus.APPLIED,
             reversible=False, resource="http"),
    ]
    consumer = Transaction(txn_id="t-down", state=TxnState.COMMITTED)
    consumer.effects = [
        _eff("t-down", 0, "read_price", status=EffectStatus.APPLIED,
             reads=[["sql", ["prices", "x"], 3]]),
    ]
    path = str(tmp_path / "mix.db")
    _write_journal(path, [t, consumer])

    with JournalReader(path) as r:
        out = r.undo_impact("t-mix")

    assert out["verdict"]["label"] == "blocked-irreversible"
    # the downstream consumer is still surfaced — the operator sees the full picture
    assert out["totals"]["committed_consumers"] == 1


# --- a transaction does not depend on itself --------------------------------


def test_self_reads_are_not_downstream(tmp_path: Path):
    """A read-modify-write inside one transaction (reads v2, writes v3, then a
    later effect reads its own v3) is not a downstream consumer of itself —
    undoing the whole txn is atomic, so its own reads don't count as a blast."""
    t = Transaction(txn_id="t-rmw", state=TxnState.COMMITTED)
    t.effects = [
        _eff("t-rmw", 0, "adjust_stock", status=EffectStatus.APPLIED,
             reads=[["sql", ["stock", "a"], 2]],
             writes=[["sql", ["stock", "a"], 3]]),
        _eff("t-rmw", 1, "read_stock", status=EffectStatus.APPLIED,
             reads=[["sql", ["stock", "a"], 3]]),  # reads its OWN write
    ]
    path = str(tmp_path / "rmw.db")
    _write_journal(path, [t])

    with JournalReader(path) as r:
        out = r.undo_impact("t-rmw")

    assert out["downstream"] == []
    assert out["superseded"] == []
    assert out["verdict"]["label"] == "clean"


# --- ordering ---------------------------------------------------------------


def test_downstream_ordered_committed_then_busiest(tmp_path: Path):
    """Committed consumers sort ahead of in-flight ones; within a tier, the
    consumer that read the most of this txn's versions comes first."""
    writer = Transaction(txn_id="t-writer", state=TxnState.COMMITTED)
    writer.effects = [
        _eff("t-writer", 0, "set_price", status=EffectStatus.APPLIED,
             writes=[["sql", ["p", "a"], 1], ["sql", ["p", "b"], 2]]),
    ]
    busy = Transaction(txn_id="t-busy", state=TxnState.COMMITTED)
    busy.effects = [
        _eff("t-busy", 0, "read", status=EffectStatus.APPLIED,
             reads=[["sql", ["p", "a"], 1], ["sql", ["p", "b"], 2]]),
    ]
    light = Transaction(txn_id="t-light", state=TxnState.COMMITTED)
    light.effects = [
        _eff("t-light", 0, "read", status=EffectStatus.APPLIED,
             reads=[["sql", ["p", "a"], 1]]),
    ]
    openish = Transaction(txn_id="t-open", state=TxnState.OPEN)
    openish.effects = [
        _eff("t-open", 0, "read", status=EffectStatus.APPLIED,
             reads=[["sql", ["p", "a"], 1], ["sql", ["p", "b"], 2]]),
    ]
    path = str(tmp_path / "order.db")
    _write_journal(path, [writer, busy, light, openish])

    with JournalReader(path) as r:
        out = r.undo_impact("t-writer")

    order = [d["txn_id"] for d in out["downstream"]]
    # committed first (busy before light by read_count), in-flight last
    assert order == ["t-busy", "t-light", "t-open"]
    assert out["verdict"]["label"] == "entangled"


# --- honest-scope caveat ----------------------------------------------------


def test_caveat_documents_action_not_data_provenance(demo_db: str):
    """The scope statement travels with the payload and is explicit that the
    relation is version-grounded action provenance, NOT proven data-flow, and
    that the read is static — not a live simulation of the undo."""
    with JournalReader(demo_db) as r:
        out = r.undo_impact("txn-clean-deploy01")
    assert out["caveat"] == UNDO_IMPACT_CAVEAT
    low = UNDO_IMPACT_CAVEAT.lower()
    assert "blast radius" in low
    assert "does not prove" in low
    assert "static" in low
    assert "irreversible" in low
