"""Adversarial-input laws: fail loud and safe, never silently wrong.

The kernel sits between an agent and prod, so hostile or malformed input must
either be rejected loudly or handled without corrupting state — the one
outcome we forbid is *silently wrong*. We fuzz the boundaries:

- **non-journal-able args** raise :class:`EffectArgsError` at the idempotency
  boundary (before anything is journalled or applied), and the world is
  untouched.
- **SQL-injection payloads** in keys/values are inert under parameterised SQL:
  stored verbatim, no table dropped or created.
- **path-traversal** strings never escape the filesystem root — write either
  stays in-root or raises.
- **oversized payloads** round-trip losslessly through commit + the audit
  journal.
- a **corrupted / truncated / unknown-status** durable journal makes
  ``recover`` fail loud (a database error, a missing-status error) or land
  ``STUCK`` for fail-safe — never a silent, lossy rollback.
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectArgsError, EffectStatus
from pherix.core.recovery import recover
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import Transaction, TxnState

from tests._laws import dump_kv, fresh_kv_conn

_LAW = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# --- malformed args ----------------------------------------------------------


@pytest.fixture
def store_tool():
    @tool(resource="sql")
    def store(conn, k, blob):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, 1),
        )
        return blob

    return store


@given(bad=st.sampled_from([object(), lambda: 1, {1, 2, 3}, complex(1, 2)]))
@_LAW
def test_non_serialisable_arg_raises_loud_and_leaves_world_untouched(store_tool, bad):
    """A non-journal-able arg is rejected at Effect construction — loud, and
    before any state change."""
    conn = fresh_kv_conn()
    try:
        before = dump_kv(conn)
        with pytest.raises(EffectArgsError):
            with agent_txn({"sql": SQLiteAdapter(conn)}):
                store_tool(k="x", blob=bad)
        assert dump_kv(conn) == before  # nothing journalled, nothing applied
    finally:
        conn.close()


# --- SQL injection -----------------------------------------------------------


@pytest.fixture
def kv_tools():
    @tool(resource="sql")
    def kv_set(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    return kv_set


_INJECTIONS = st.one_of(
    st.text(max_size=40),
    st.sampled_from(
        [
            "'; DROP TABLE kv; --",
            "x') ; DELETE FROM kv; --",
            "1 OR 1=1",
            "'; CREATE TABLE evil(x); --",
            'robert"); DROP TABLE kv;--',
        ]
    ),
)


@given(key=_INJECTIONS)
@_LAW
def test_injection_payload_is_stored_verbatim(kv_tools, key):
    """Parameterised SQL renders an injection inert: the payload is data, not
    code — stored verbatim, with no table dropped or created."""
    conn = fresh_kv_conn()
    try:
        tables_before = _tables(conn)
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            kv_tools(k=key, v=7)
        # The key round-tripped as a literal string; the schema is intact.
        assert dump_kv(conn) == {key: 7}
        assert _tables(conn) == tables_before
    finally:
        conn.close()


def _tables(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_pherix_%'"
        )
    }


# --- path traversal ----------------------------------------------------------


@given(
    rel=st.lists(
        st.sampled_from(["..", "a", "b", "sub", "x.txt", ".", "etc"]),
        min_size=1,
        max_size=5,
    )
)
@_LAW
def test_fs_writes_never_escape_root(rel, tmp_path_factory):
    """A generated path either resolves inside root or is rejected — it can
    never write outside the root, however many ``..`` segments it carries."""
    root = tmp_path_factory.mktemp("fsroot")
    adapter = FilesystemAdapter(root)
    adapter.begin()
    try:
        handle = FsHandle(root.resolve(), root.resolve(), {})
        path = "/".join(rel)
        try:
            handle.write(path, b"data")
        except (ValueError, OSError):
            # Rejected loudly: ValueError for an escape attempt, OSError (e.g.
            # IsADirectoryError when the path resolves to root itself). Either
            # way nothing was written outside root.
            return
        # If it succeeded, the written file must be inside root.
        written = (root.resolve() / path).resolve()
        assert written.is_relative_to(root.resolve())
    finally:
        adapter.rollback()


# --- oversized payload -------------------------------------------------------


@pytest.fixture
def blob_store():
    @tool(resource="sql")
    def put(conn, k, big):
        conn.execute(
            "INSERT INTO blobs (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, big),
        )
        return len(big)

    return put


def test_oversized_payload_round_trips_losslessly(blob_store):
    """A multi-megabyte value commits and round-trips through the journal and
    the audit log without truncation or corruption."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE blobs (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    big = "A" * (2 * 1024 * 1024)  # 2 MiB
    audit = AuditJournal.in_memory()
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
            blob_store(k="huge", big=big)
        stored = conn.execute("SELECT v FROM blobs WHERE k='huge'").fetchone()[0]
        assert stored == big  # no truncation in the resource
        # The audit row faithfully persisted the full args payload.
        eff = audit.get_effects(txn.txn_id)[0]
        assert big in eff["args"]
    finally:
        conn.close()
        audit.close()


# --- corrupted / truncated / unknown-status durable journal ------------------


def test_recover_on_corrupted_file_fails_loud(tmp_path):
    """A non-SQLite file handed to recover raises a database error — it must
    never be mistaken for an empty journal and silently succeed."""
    bad = tmp_path / "garbage.db"
    bad.write_bytes(b"this is definitely not a sqlite database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        recover(str(bad), {})


def test_recover_on_unknown_effect_status_fails_loud(tmp_path):
    """An effect row carrying a status outside the EffectStatus enum is a
    corrupt journal — recovery raises rather than silently skipping it."""
    db_path = str(tmp_path / "j.db")
    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = TxnState.PARTIAL
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, TxnState.PARTIAL.name)
    # index 0 keeps the txn mid-flight (a real APPLIED effect).
    applied = Effect(
        txn_id=txn.txn_id, index=0, tool="t", args={}, resource="ext",
        reversible=False, status=EffectStatus.APPLIED,
    )
    audit.record_effect(applied)
    audit.update_effect(applied)
    # index 1 carries a bogus status injected directly into the durable row.
    bogus = Effect(
        txn_id=txn.txn_id, index=1, tool="t", args={}, resource="ext",
        reversible=False, status=EffectStatus.APPLIED,
    )
    audit.record_effect(bogus)
    audit._conn.execute(
        "UPDATE effects SET status = ? WHERE txn_id = ? AND idx = ?",
        ("BOGUS_STATUS", txn.txn_id, 1),
    )
    audit._conn.commit()
    audit.close()

    with pytest.raises(KeyError):
        recover(db_path, {"ext": _NullIrreversibleAdapter()})


def test_recover_unregistered_tool_lands_stuck_not_silent(tmp_path):
    """A standing irreversible whose tool is no longer registered cannot be
    compensated — recovery lands STUCK (fail-safe), never a silent rollback
    that would imply the side effect was undone when it was not."""
    db_path = str(tmp_path / "j.db")
    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = TxnState.PARTIAL
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, TxnState.PARTIAL.name)
    standing = Effect(
        txn_id=txn.txn_id, index=0, tool="ghost_charge_never_registered",
        args={"amount": 50}, resource="ext", reversible=False,
        status=EffectStatus.APPLIED,
    )
    audit.record_effect(standing)
    audit.update_effect(standing)
    audit.close()

    report = recover(db_path, {"ext": _NullIrreversibleAdapter()})
    assert len(report.transactions) == 1
    assert report.transactions[0].final_state == TxnState.STUCK.name
    assert report.compensators_fired == 0


def test_recover_truncated_journal_is_safe_noop(tmp_path):
    """A transaction row with no effects at all (a journal truncated before any
    effect was written) is not mid-flight — recovery is a clean no-op, not a
    crash."""
    db_path = str(tmp_path / "j.db")
    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = TxnState.OPEN
    audit.record_transaction(txn)
    audit.close()

    report = recover(db_path, {})
    assert report.transactions == []


class _NullIrreversibleAdapter:
    """Irreversible adapter that would fire a compensator if asked — used only
    so the resource resolves; the adversarial journals never reach a real
    compensator fire."""

    name = "ext"

    def supports_rollback(self) -> bool:
        return False

    def apply(self, effect, tool_fn):  # pragma: no cover - never reached
        return tool_fn(**effect.args)
