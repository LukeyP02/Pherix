"""Slice 4 / Stream B: automatic read/write-set capture through resource handles.

These tests pin down the *recording side* of isolation — the bookkeeping the
runtime relies on at commit time. They do not exercise the runtime: each test
sets ``active_effect`` directly (the runtime's job in Stream C) so the
recording logic can be verified in pure pre-conditions / post-conditions.

The journal-shape contract under test (locked across all three streams):

  * ``effect.read_keys`` entry: ``(resource: str, key: tuple, version: object)``
  * ``effect.write_keys`` entry: ``(resource: str, key: tuple)`` — no version

  * SQL keys: ``(table_name, pk_value)``
  * FS keys:  ``(rel_path,)``

The handle / helper are responsible for:

  * computing the version (via ``adapter.read_version`` for reads, and bumping
    via ``adapter.write_version`` for writes),
  * deduping repeat records within one effect,
  * graceful no-op when ``active_effect`` is None (raw / out-of-txn tests).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.effects import Effect
from pherix.core.tools import active_effect


# --- fixtures --------------------------------------------------------------


def _make_effect(resource: str = "fs") -> Effect:
    """Synthesise a fresh Effect — runtime-independent."""
    return Effect(
        txn_id="t",
        index=0,
        tool="probe",
        args={},
        resource=resource,
        reversible=True,
    )


@pytest.fixture
def fs_adapter(tmp_path: Path) -> FilesystemAdapter:
    root = tmp_path / "root"
    root.mkdir()
    a = FilesystemAdapter(root)
    a.begin()
    yield a
    # rollback() is the cleanup that does not assume commit() ran.
    a.rollback()


@pytest.fixture
def fs_handle_for(fs_adapter: FilesystemAdapter):
    """Build a handle bound to a given Effect, like the runtime would."""

    def _build(effect: Effect):
        effect.snapshot = fs_adapter.snapshot(effect)
        token = active_effect.set(effect)
        try:
            return fs_adapter._handle_for(effect.snapshot), token
        except Exception:
            active_effect.reset(token)
            raise

    return _build


@pytest.fixture
def sqlite_adapter() -> SQLiteAdapter:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE counters (id TEXT PRIMARY KEY, n INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO counters (id, n) VALUES ('x', 0)")
    return SQLiteAdapter(conn)


# --- FsHandle: read records ------------------------------------------------


def test_fs_handle_read_records_read_key_with_content_hash(
    fs_adapter, fs_handle_for
):
    # Seed a file outside the handle so the hash is known before the read.
    (fs_adapter.root / "foo.txt").write_bytes(b"hello")
    effect = _make_effect()
    handle, token = fs_handle_for(effect)
    try:
        assert handle.read("foo.txt") == b"hello"
        assert len(effect.read_keys) == 1
        resource, key, version = effect.read_keys[0]
        assert resource == "fs"
        assert key == ("foo.txt",)
        # version is the sha256 of the file content, not a sentinel
        assert version != "__missing__"
        assert isinstance(version, str) and len(version) == 64
    finally:
        active_effect.reset(token)


def test_fs_handle_read_dedupes_within_one_effect(fs_adapter, fs_handle_for):
    (fs_adapter.root / "foo.txt").write_bytes(b"hello")
    effect = _make_effect()
    handle, token = fs_handle_for(effect)
    try:
        handle.read("foo.txt")
        handle.read("foo.txt")
        handle.read("foo.txt")
        # Three reads, one read_key entry.
        assert len(effect.read_keys) == 1
    finally:
        active_effect.reset(token)


# --- FsHandle: write records -----------------------------------------------


def test_fs_handle_write_records_write_key(fs_adapter, fs_handle_for):
    effect = _make_effect()
    handle, token = fs_handle_for(effect)
    try:
        handle.write("foo.txt", b"hello")
        assert effect.write_keys == [("fs", ("foo.txt",))]
    finally:
        active_effect.reset(token)


def test_fs_handle_write_dedupes_within_one_effect(
    fs_adapter, fs_handle_for
):
    effect = _make_effect()
    handle, token = fs_handle_for(effect)
    try:
        handle.write("foo.txt", b"a")
        handle.write("foo.txt", b"bb")
        handle.write("foo.txt", b"ccc")
        # Three writes, one write_key entry — the journal stays compact.
        # The disk content reflects the *last* write.
        assert effect.write_keys == [("fs", ("foo.txt",))]
        assert (fs_adapter.root / "foo.txt").read_bytes() == b"ccc"
    finally:
        active_effect.reset(token)


def test_fs_handle_delete_records_write_key(fs_adapter, fs_handle_for):
    (fs_adapter.root / "foo.txt").write_bytes(b"hello")
    effect = _make_effect()
    handle, token = fs_handle_for(effect)
    try:
        handle.delete("foo.txt")
        assert effect.write_keys == [("fs", ("foo.txt",))]
    finally:
        active_effect.reset(token)


# --- FsHandle: graceful no-op when active_effect is None ------------------


def test_fs_handle_no_recording_outside_active_effect(fs_adapter):
    # No active_effect.set — the handle must still function (the Slice 2
    # raw-handle behaviour) but record nothing into any Effect.
    effect = _make_effect()
    effect.snapshot = fs_adapter.snapshot(effect)
    handle = fs_adapter._handle_for(effect.snapshot)
    # The handle was built when active_effect was None — recording is a
    # no-op even though we have an Effect lying around.
    (fs_adapter.root / "foo.txt").write_bytes(b"hello")
    assert handle.read("foo.txt") == b"hello"
    handle.write("bar.txt", b"data")
    assert effect.read_keys == []
    assert effect.write_keys == []


def test_fs_handle_mixed_read_write_records_both(fs_adapter, fs_handle_for):
    (fs_adapter.root / "a.txt").write_bytes(b"hello")
    effect = _make_effect()
    handle, token = fs_handle_for(effect)
    try:
        handle.read("a.txt")
        handle.write("b.txt", b"new")
        handle.delete("a.txt")
        assert len(effect.read_keys) == 1
        assert effect.read_keys[0][:2] == ("fs", ("a.txt",))
        # Both b.txt (write) and a.txt (delete) recorded as write_keys.
        assert ("fs", ("b.txt",)) in effect.write_keys
        assert ("fs", ("a.txt",)) in effect.write_keys
        assert len(effect.write_keys) == 2
    finally:
        active_effect.reset(token)


# --- execute_isolated: SQL read records ------------------------------------


def test_execute_isolated_records_read_with_current_version(sqlite_adapter):
    effect = _make_effect(resource="sql")
    token = active_effect.set(effect)
    try:
        execute_isolated(
            sqlite_adapter.conn,
            "SELECT n FROM counters WHERE id = ?",
            ("x",),
            reads=[("counters", "x")],
        )
        assert len(effect.read_keys) == 1
        resource, key, version = effect.read_keys[0]
        assert resource == "sql"
        assert key == ("counters", "x")
        # Never written → version 0 sentinel.
        assert version == 0
    finally:
        active_effect.reset(token)


def test_execute_isolated_records_write_and_bumps_version(sqlite_adapter):
    effect = _make_effect(resource="sql")
    token = active_effect.set(effect)
    try:
        # Pre-state: version side-table empty for ("counters", "x").
        assert sqlite_adapter.read_version(("counters", "x")) == 0
        execute_isolated(
            sqlite_adapter.conn,
            "UPDATE counters SET n = n + 1 WHERE id = ?",
            ("x",),
            writes=[("counters", "x")],
        )
        # Write recorded, version bumped.
        assert effect.write_keys == [("sql", ("counters", "x"))]
        assert sqlite_adapter.read_version(("counters", "x")) == 1
    finally:
        active_effect.reset(token)


def test_execute_isolated_outside_active_effect_is_noop_recording(
    sqlite_adapter,
):
    # No active_effect — the helper still runs the stmt but records nothing
    # and does not crash. The version side-table is also untouched.
    pre = sqlite_adapter.read_version(("counters", "x"))
    cur = execute_isolated(
        sqlite_adapter.conn,
        "UPDATE counters SET n = n + 1 WHERE id = ?",
        ("x",),
        writes=[("counters", "x")],
    )
    # The actual SQL ran (so the row was updated)…
    row = sqlite_adapter.conn.execute(
        "SELECT n FROM counters WHERE id = 'x'"
    ).fetchone()
    assert row[0] == 1
    # …but no version bump (no agent_txn means no isolation bookkeeping).
    assert sqlite_adapter.read_version(("counters", "x")) == pre
    assert isinstance(cur, sqlite3.Cursor)


def test_execute_isolated_multi_write_bumps_each(sqlite_adapter):
    sqlite_adapter.conn.execute(
        "INSERT INTO counters (id, n) VALUES ('y', 0), ('z', 0)"
    )
    effect = _make_effect(resource="sql")
    token = active_effect.set(effect)
    try:
        execute_isolated(
            sqlite_adapter.conn,
            "UPDATE counters SET n = n + 1 WHERE id IN ('x','y','z')",
            (),
            writes=[("counters", "x"), ("counters", "y"), ("counters", "z")],
        )
        # All three write_keys present (order preserved).
        assert effect.write_keys == [
            ("sql", ("counters", "x")),
            ("sql", ("counters", "y")),
            ("sql", ("counters", "z")),
        ]
        # All three versions bumped to 1.
        for pk in ("x", "y", "z"):
            assert sqlite_adapter.read_version(("counters", pk)) == 1
    finally:
        active_effect.reset(token)


def test_execute_isolated_dedupes_within_one_effect(sqlite_adapter):
    effect = _make_effect(resource="sql")
    token = active_effect.set(effect)
    try:
        execute_isolated(
            sqlite_adapter.conn,
            "SELECT n FROM counters WHERE id = ?",
            ("x",),
            reads=[("counters", "x")],
        )
        execute_isolated(
            sqlite_adapter.conn,
            "SELECT n FROM counters WHERE id = ?",
            ("x",),
            reads=[("counters", "x")],
        )
        # Two reads of the same key — one read_key entry.
        assert len(effect.read_keys) == 1
    finally:
        active_effect.reset(token)


def test_execute_isolated_no_adapter_on_connection_is_noop_recording():
    # A bare sqlite3.Connection that was *not* wrapped by SQLiteAdapter →
    # the helper can't find an adapter, so it falls back to plain execute
    # with no recording. Important so the helper degrades gracefully if a
    # tool is mis-wired rather than crashing the txn.
    raw = sqlite3.connect(":memory:", isolation_level=None)
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    effect = _make_effect(resource="sql")
    token = active_effect.set(effect)
    try:
        execute_isolated(
            raw,
            "INSERT INTO t (id) VALUES (?)",
            (1,),
            writes=[("t", 1)],
        )
        assert effect.read_keys == []
        assert effect.write_keys == []
        row = raw.execute("SELECT id FROM t").fetchone()
        assert row[0] == 1
    finally:
        active_effect.reset(token)


def test_execute_isolated_returns_cursor_with_query_results(sqlite_adapter):
    effect = _make_effect(resource="sql")
    token = active_effect.set(effect)
    try:
        cur = execute_isolated(
            sqlite_adapter.conn,
            "SELECT n FROM counters WHERE id = ?",
            ("x",),
            reads=[("counters", "x")],
        )
        row = cur.fetchone()
        assert row[0] == 0
    finally:
        active_effect.reset(token)
