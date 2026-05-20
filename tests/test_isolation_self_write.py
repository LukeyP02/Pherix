"""Slice 4 — read-then-write the same key must not self-conflict.

The bug (found by the audit dogfood): a transaction that reads a key and
then writes the same key, against an **on-disk** SQLite database, raised a
spurious :class:`IsolationConflict` at commit — message "read v0, now v0" —
even though no other transaction touched the key.

Root cause: the commit-time diff expected the live version to equal the
version *after my own write* (``last_my_write``), but on-disk
``read_version`` goes through a committed-only meta connection that cannot
see my own uncommitted write, so it reported the pre-write committed
version. ``last_my_write != committed_base`` → false conflict.

The fix reconciles the two ``read_version`` visibilities:

  * **committed-only** (on-disk, meta connection): my own uncommitted
    writes are invisible at read time AND at commit time, so they cancel —
    the diff compares the committed base at read (``v_at_read``) against the
    committed base now (``v_now``). ``SQLiteAdapter.reads_committed_only()``
    returns True here.
  * **own-write-visible** (``:memory:`` main connection, FakeAdapter, FS):
    ``read_version`` reflects my own bumps, so the diff keeps using
    ``last_my_write``. The default branch — unchanged by the fix.

This file pins the TASK matrix. One structural fact discovered while
writing it: rows that demand "read-then-write the SAME key WHILE another
txn commits a write to that same key" cannot be staged with two *live*
SQLite writers on one database — SQLite's own single-writer / snapshot
isolation serializes them and the second read-modify-write fails with
"database is locked" *before* Pherix's commit-time diff ever runs (see
:func:`test_sqlite_serializes_same_key_writers_below_pherix`). The same-key
lost update is therefore caught one layer below Pherix; Pherix's version
diff is the net for the cases SQLite does NOT serialize (a key I only read,
cross-resource, cross-process). So matrix row #2 — "P3's protection must
survive for a read-then-written key" — is pinned at the diff level against
a committed-only adapter (the algorithm), while the live two-adapter and
cross-process pins exercise the read-only-then-decide case end-to-end.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pherix.core.effects import Effect
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.isolation import Abort, IsolationConflict, check_conflicts
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY as TOOL_REGISTRY, tool
from pherix.core.transaction import TxnState


# --- fixtures / helpers ------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tool_registry():
    """Restore the tool registry between tests so @tool inside a function
    doesn't bleed across files."""
    snapshot = dict(TOOL_REGISTRY._tools)
    yield
    TOOL_REGISTRY._tools = snapshot


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER)")
    conn.execute("INSERT INTO counters VALUES ('x', 0)")


@pytest.fixture
def on_disk_db(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    boot = sqlite3.connect(str(db), isolation_level=None)
    boot.execute("PRAGMA journal_mode=WAL")
    _seed(boot)
    boot.close()
    return db


def _open_adapter(db: Path) -> tuple[sqlite3.Connection, SQLiteAdapter]:
    conn = sqlite3.connect(str(db), isolation_level=None)
    return conn, SQLiteAdapter(conn)


def _rw_tools():
    @tool(resource="sql")
    def read_x(conn, name):
        cur = execute_isolated(
            conn,
            "SELECT val FROM counters WHERE name = ?",
            (name,),
            reads=[("counters", name)],
        )
        return cur.fetchone()[0]

    @tool(resource="sql")
    def write_x(conn, name, val):
        execute_isolated(
            conn,
            "UPDATE counters SET val = ? WHERE name = ?",
            (val, name),
            writes=[("counters", name)],
        )

    return read_x, write_x


# === ON-DISK (committed-only path: SQLiteAdapter.reads_committed_only) =======


def test_on_disk_read_then_write_same_key_no_other_writer_does_not_conflict(
    on_disk_db: Path,
):
    """Matrix #1 — THE BUG. read x, write x, real on-disk DB, nothing else
    touching the key → commit cleanly. Previously raised the
    self-contradictory 'read v0, now v0' IsolationConflict."""
    conn, adapter = _open_adapter(on_disk_db)
    try:
        read_x, write_x = _rw_tools()
        with agent_txn({"sql": adapter}, isolation=Abort()) as ctx:
            assert read_x(name="x") == 0
            write_x(name="x", val=5)
        assert ctx.txn.state is TxnState.COMMITTED
        post = sqlite3.connect(str(on_disk_db)).execute(
            "SELECT val FROM counters WHERE name = 'x'"
        ).fetchone()[0]
        assert post == 5
    finally:
        conn.close()


def test_on_disk_read_only_no_other_writer_does_not_conflict(on_disk_db: Path):
    """Matrix #3 — read-only key, nobody else writes → no conflict."""
    conn, adapter = _open_adapter(on_disk_db)
    try:
        read_x, _ = _rw_tools()
        with agent_txn({"sql": adapter}, isolation=Abort()) as ctx:
            assert read_x(name="x") == 0
        assert ctx.txn.state is TxnState.COMMITTED
    finally:
        conn.close()


def test_on_disk_read_only_with_concurrent_committed_write_conflicts(
    on_disk_db: Path,
):
    """Matrix #4 — read-only key, ANOTHER txn commits a write to it →
    conflict. A never writes the key, so it holds only a read lock — B
    can write+commit freely, and A's commit-time meta read sees the moved
    committed base. This is P3's protection on the real on-disk adapter."""
    conn_a, ad_a = _open_adapter(on_disk_db)
    conn_b, ad_b = _open_adapter(on_disk_db)
    try:
        read_x, write_x = _rw_tools()
        with pytest.raises(IsolationConflict) as info:
            with agent_txn({"sql": ad_a}, isolation=Abort()) as ctx_a:
                assert read_x(name="x") == 0
                with agent_txn({"sql": ad_b}) as _:
                    write_x(name="x", val=99)
        c = info.value.conflicts[0]
        assert c.resource == "sql"
        assert c.key == ("counters", "x")
        assert c.version_at_read == 0
        assert c.version_expected == 0  # committed base at read
        assert c.version_now == 1  # B's committed bump
        assert ctx_a.txn.state is TxnState.ROLLED_BACK
    finally:
        conn_a.close()
        conn_b.close()


def test_on_disk_read_then_write_with_external_commit_conflicts_diff_level():
    """Matrix #2 — P3's protection must survive for a read-then-WRITTEN key.

    Two *live* SQLite writers cannot race the same key (SQLite serializes
    them — see test_sqlite_serializes_same_key_writers_below_pherix), so
    this pins the algorithm at the commit-time diff against a committed-only
    adapter: I read x@0, I write x (my own bump → invisible to the meta
    read), AND another txn commits a write (committed base → 1). The diff
    must compare the committed base now (1) against the committed base at
    read (0) and fire — NOT swallow it as a self-bump.
    """

    @dataclass
    class CommittedOnlyAdapter:
        """A read_version that, like the on-disk meta connection, excludes
        this txn's own uncommitted writes (reads_committed_only → True)."""

        name: str = "sql"
        committed: dict = field(default_factory=dict)

        def supports_rollback(self) -> bool:
            return True

        def reads_committed_only(self) -> bool:
            return True

        def read_version(self, key: tuple) -> Any:
            return self.committed.get(tuple(key), 0)

        def snapshot(self, effect):  # pragma: no cover - shape only
            raise NotImplementedError

        def apply(self, effect, tool_fn):  # pragma: no cover - shape only
            raise NotImplementedError

        def restore(self, handle):  # pragma: no cover - shape only
            raise NotImplementedError

    def eff(read_keys, write_keys):
        return Effect(
            txn_id="t",
            index=0,
            tool="x",
            args={},
            resource="sql",
            reversible=True,
            read_keys=read_keys,
            write_keys=write_keys,
        )

    key = ("counters", "x")
    rk = [("sql", key, 0)]  # read at committed base 0
    wk = [("sql", key, 1)]  # my own write bumped my main-conn view to 1

    # No external writer: committed base still 0 → my self-bump must NOT flag.
    quiet = CommittedOnlyAdapter(committed={key: 0})
    assert check_conflicts([eff(rk, wk)], {"sql": quiet}) == []

    # Another txn committed a write: committed base now 1 → conflict (P3).
    moved = CommittedOnlyAdapter(committed={key: 1})
    conflicts = check_conflicts([eff(rk, wk)], {"sql": moved})
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.key == key
    assert c.version_at_read == 0
    assert c.version_expected == 0  # committed base at read, NOT last_my_write
    assert c.version_now == 1


def test_sqlite_serializes_same_key_writers_below_pherix(on_disk_db: Path):
    """Matrix #2/#6 structural note. On one on-disk DB, a second
    transaction cannot read-modify-write a key another open txn already
    wrote — SQLite raises 'database is locked' at the write, before
    Pherix's commit diff runs. The same-key lost update is prevented one
    layer below Pherix; this test documents that boundary so the matrix's
    'two live writers, same key' rows are understood, not silently absent.
    """
    conn_a, ad_a = _open_adapter(on_disk_db)
    conn_b, ad_b = _open_adapter(on_disk_db)
    try:
        read_x, write_x = _rw_tools()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with agent_txn({"sql": ad_a}, isolation=Abort()):
                read_x(name="x")
                write_x(name="x", val=5)  # A acquires the write lock
                with agent_txn({"sql": ad_b}):
                    write_x(name="x", val=99)  # B cannot get it → locked
    finally:
        conn_a.close()
        conn_b.close()


# === :memory: (own-write-visible path: reads_committed_only → False) =========
#
# These prove the fix left the main-connection path untouched — the same
# read_version/write_version/diff machinery the FakeAdapter unit pins
# exercise, but through a real in-memory SQLiteAdapter. A private :memory:
# DB is single-connection by definition, so a concurrent committed writer
# is modelled by a direct write_version bump on the shared side-table (the
# same row another writer would touch) — exactly what the on-disk meta path
# would observe, but on the own-write-visible connection.


@pytest.fixture
def mem_adapter():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    _seed(conn)
    adapter = SQLiteAdapter(conn)
    assert adapter.reads_committed_only() is False  # main-connection path
    yield adapter
    conn.close()


def test_memory_read_then_write_same_key_no_other_writer_does_not_conflict(
    mem_adapter,
):
    """Matrix #5 / #1 on the main-connection path. read x, write x → on
    :memory: read_version sees my own bump, last_my_write == live version
    → no conflict."""
    read_x, write_x = _rw_tools()
    with agent_txn({"sql": mem_adapter}, isolation=Abort()) as ctx:
        assert read_x(name="x") == 0
        write_x(name="x", val=5)
    assert ctx.txn.state is TxnState.COMMITTED


def test_memory_read_then_write_same_key_with_external_commit_conflicts(
    mem_adapter,
):
    """Matrix #5 / #2 on the main-connection path. read x, write x, then a
    concurrent committed writer bumps the same side-table row beyond my last
    write → live version > last_my_write → conflict (P3 survives here too)."""
    read_x, write_x = _rw_tools()
    with pytest.raises(IsolationConflict) as info:
        with agent_txn({"sql": mem_adapter}, isolation=Abort()):
            assert read_x(name="x") == 0
            write_x(name="x", val=5)  # my bump → version 1
            # Another committed writer touches the same key → version 2,
            # which is NOT in my journal's write_keys.
            mem_adapter.write_version(("counters", "x"))
    c = info.value.conflicts[0]
    assert c.key == ("counters", "x")
    assert c.version_expected == 1  # last_my_write on this path
    assert c.version_now == 2


def test_memory_read_only_no_other_writer_does_not_conflict(mem_adapter):
    """Matrix #5 / #3 on the main-connection path."""
    read_x, _ = _rw_tools()
    with agent_txn({"sql": mem_adapter}, isolation=Abort()) as ctx:
        assert read_x(name="x") == 0
    assert ctx.txn.state is TxnState.COMMITTED


def test_memory_read_only_with_external_commit_conflicts(mem_adapter):
    """Matrix #5 / #4 on the main-connection path. read x (only), then a
    concurrent committed writer bumps it → expected v_at_read=0, now=1."""
    read_x, _ = _rw_tools()
    with pytest.raises(IsolationConflict) as info:
        with agent_txn({"sql": mem_adapter}, isolation=Abort()):
            assert read_x(name="x") == 0
            mem_adapter.write_version(("counters", "x"))  # external commit
    c = info.value.conflicts[0]
    assert c.version_at_read == 0
    assert c.version_expected == 0
    assert c.version_now == 1


# === cross-process (matrix #6) ===============================================


_CHILD = textwrap.dedent("""
    import sqlite3, sys
    db_path = sys.argv[1]
    from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
    from pherix.core.runtime import agent_txn
    from pherix.core.tools import tool

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    adapter = SQLiteAdapter(conn)

    @tool(resource="sql")
    def credit(conn, name, amount):
        execute_isolated(
            conn,
            "UPDATE counters SET val = val + ? WHERE name = ?",
            (amount, name),
            writes=[("counters", name)],
        )

    with agent_txn({"sql": adapter}) as ctx:
        credit(name="x", amount=1000)
    print(ctx.txn.state.name)
""")


def test_cross_process_read_then_no_writer_does_not_conflict(on_disk_db: Path):
    """Matrix #6 (negative) — parent reads x on the on-disk file, no other
    process writes → no conflict, commit clean."""
    conn, adapter = _open_adapter(on_disk_db)
    try:
        read_x, _ = _rw_tools()
        with agent_txn({"sql": adapter}, isolation=Abort()) as ctx:
            assert read_x(name="x") == 0
        assert ctx.txn.state is TxnState.COMMITTED
    finally:
        conn.close()


def test_cross_process_concurrent_committed_write_conflicts(on_disk_db: Path):
    """Matrix #6 (positive) — parent reads x; a separate Python process
    opens its own agent_txn and commits a write to x; parent's commit-time
    meta read sees the cross-process bump and IsolationConflict fires.

    Read-only parent: a read-then-write parent would be serialized by
    SQLite at its own write step (see the structural-note test), so the
    Pherix-diff-active case is the read-then-decide one — which is exactly
    the cross-process net SQLite's single-DB locking can't provide on its
    own here (the parent never re-writes the row)."""
    conn, adapter = _open_adapter(on_disk_db)
    try:
        read_x, _ = _rw_tools()
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)}
        with pytest.raises(IsolationConflict) as info:
            with agent_txn({"sql": adapter}, isolation=Abort()):
                assert read_x(name="x") == 0
                result = subprocess.run(
                    [sys.executable, "-c", _CHILD, str(on_disk_db)],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                )
                if result.returncode != 0:
                    pytest.fail(
                        f"child failed:\nout={result.stdout}\nerr={result.stderr}"
                    )
                assert result.stdout.strip().split("\n")[-1] == "COMMITTED"
        c = info.value.conflicts[0]
        assert c.key == ("counters", "x")
        assert c.version_at_read == 0
        assert c.version_expected == 0
        assert c.version_now == 1
    finally:
        conn.close()


# === message improvement (matrix #7) ========================================


def test_conflict_message_surfaces_the_expected_version(on_disk_db: Path):
    """Matrix #7 — the message includes the version actually compared
    against (``expected``), so a future false positive is self-explaining
    rather than reading the misleading 'read v0, now v0'."""
    conn_a, ad_a = _open_adapter(on_disk_db)
    conn_b, ad_b = _open_adapter(on_disk_db)
    try:
        read_x, write_x = _rw_tools()
        with pytest.raises(IsolationConflict) as info:
            with agent_txn({"sql": ad_a}, isolation=Abort()):
                read_x(name="x")
                with agent_txn({"sql": ad_b}) as _:
                    write_x(name="x", val=99)
        msg = str(info.value)
        assert "read v0" in msg
        assert "expected v0" in msg
        assert "now v1" in msg
    finally:
        conn_a.close()
        conn_b.close()
