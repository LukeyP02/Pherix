import sqlite3

import pytest

from pherix.core.adapters.sql import (
    _MATERIALISE_MAX_DEPTH,
    SQLiteAdapter,
    _materialise_cursor,
)
from pherix.core.effects import Effect


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def insert_user(conn, name):
    cur = conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    return cur.lastrowid


def count_users(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def effect(index, tool, args):
    return Effect(
        txn_id="t", index=index, tool=tool, args=args, resource="sql", reversible=True
    )


def test_supports_rollback(conn):
    assert SQLiteAdapter(conn).supports_rollback() is True


def test_snapshot_apply_restore_round_trip(conn):
    a = SQLiteAdapter(conn)
    a.begin()
    e = effect(0, "insert_user", {"name": "bob"})
    handle = a.snapshot(e)
    a.apply(e, insert_user)
    assert count_users(conn) == 1
    a.restore(handle)
    assert count_users(conn) == 0


def test_apply_returns_tool_result(conn):
    a = SQLiteAdapter(conn)
    a.begin()
    e = effect(0, "insert_user", {"name": "bob"})
    a.snapshot(e)
    assert a.apply(e, insert_user) == 1


def test_apply_injects_connection_as_first_arg(conn):
    seen = {}

    def spy(c, name):
        seen["conn"] = c

    a = SQLiteAdapter(conn)
    a.begin()
    e = effect(0, "spy", {"name": "x"})
    a.snapshot(e)
    a.apply(e, spy)
    assert seen["conn"] is conn


def test_savepoint_name_derives_from_effect_index(conn):
    a = SQLiteAdapter(conn)
    a.begin()
    h = a.snapshot(effect(5, "insert_user", {"name": "bob"}))
    assert h.payload["savepoint"] == "sp_5"


def test_restore_newest_first_unwinds_in_reverse(conn):
    a = SQLiteAdapter(conn)
    a.begin()
    handles = []
    for i, name in enumerate(["a", "b", "c"]):
        e = effect(i, "insert_user", {"name": name})
        handles.append(a.snapshot(e))
        a.apply(e, insert_user)
    assert count_users(conn) == 3
    # backward fold: restore the newest savepoint first
    a.restore(handles[2])
    assert count_users(conn) == 2
    a.restore(handles[1])
    assert count_users(conn) == 1
    a.restore(handles[0])
    assert count_users(conn) == 0


def test_commit_persists_across_transactions(conn):
    a = SQLiteAdapter(conn)
    a.begin()
    e = effect(0, "insert_user", {"name": "bob"})
    a.snapshot(e)
    a.apply(e, insert_user)
    a.commit()
    a.begin()
    assert count_users(conn) == 1
    a.rollback()


def test_outer_rollback_discards_everything(conn):
    a = SQLiteAdapter(conn)
    a.begin()
    for i, name in enumerate(["a", "b"]):
        e = effect(i, "insert_user", {"name": name})
        a.snapshot(e)
        a.apply(e, insert_user)
    a.rollback()
    a.begin()
    assert count_users(conn) == 0
    a.rollback()


# --- _materialise_cursor: cursors are drained wherever they appear ----------
# A reader tool can hand back a one-shot sqlite3.Cursor — at the top level or
# wrapped in a dict/list/tuple alongside metadata. Any cursor that survives to
# effect.result is rejected by the strict journal dump and kills the txn. The
# materialiser drains them all into journal-safe, re-iterable rows, mirroring
# the TS executeIsolated (which returns rows via .all()).


def _select_cursor(conn, ids=("bob",)):
    """A live, unconsumed SELECT cursor over the seeded users table."""
    for name in ids:
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    return conn.execute("SELECT id, name FROM users ORDER BY id")


def test_materialise_top_level_cursor_drains_to_rows(conn):
    cur = _select_cursor(conn, ("bob",))
    assert _materialise_cursor(cur) == [[1, "bob"]]


def test_materialise_passes_non_cursor_leaves_through():
    for value in (None, 7, "x", 3.5, b"bytes", True):
        assert _materialise_cursor(value) is value


def test_materialise_drains_cursor_nested_in_dict(conn):
    # The natural rows-plus-metadata shape: {"rows": cursor, "n": 1}.
    cur = _select_cursor(conn, ("bob",))
    out = _materialise_cursor({"rows": cur, "n": 1})
    assert out == {"rows": [[1, "bob"]], "n": 1}


def test_materialise_drains_cursor_nested_in_list(conn):
    cur = _select_cursor(conn, ("bob",))
    assert _materialise_cursor([cur, "tail"]) == [[[1, "bob"]], "tail"]


def test_materialise_drains_cursor_nested_in_tuple_preserving_type(conn):
    cur = _select_cursor(conn, ("bob",))
    out = _materialise_cursor(("head", cur))
    assert out == ("head", [[1, "bob"]])
    assert isinstance(out, tuple)


def test_materialise_drains_deeply_nested_cursor(conn):
    cur = _select_cursor(conn, ("bob",))
    out = _materialise_cursor({"a": [{"b": (cur,)}]})
    assert out == {"a": [{"b": ([[1, "bob"]],)}]}


def test_materialise_preserves_row_factory_column_names(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO users (name) VALUES (?)", ("bob",))
    cur = conn.execute("SELECT id, name FROM users")
    assert _materialise_cursor({"rows": cur}) == {"rows": [{"id": 1, "name": "bob"}]}


def test_materialise_depth_cap_stops_recursing(conn):
    # Past the cap the value is returned unchanged (no rescue that deep) — the
    # strict journal dump would then reject a stray cursor loudly, as before.
    cur = _select_cursor(conn, ("bob",))
    nested = cur
    for _ in range(_MATERIALISE_MAX_DEPTH + 1):
        nested = [nested]
    out = _materialise_cursor(nested)
    # Walk down to the leaf: it is still the raw cursor, not drained rows.
    leaf = out
    while isinstance(leaf, list):
        leaf = leaf[0]
    assert isinstance(leaf, sqlite3.Cursor)


def test_apply_drains_cursor_nested_in_tool_result(conn):
    # End-to-end at the adapter boundary: apply() runs the tool and must hand
    # back a journal-safe result even when the tool nests a cursor.
    def reader(c, name):
        cur = c.execute("SELECT id, name FROM users WHERE name = ?", (name,))
        return {"rows": cur, "ok": True}

    a = SQLiteAdapter(conn)
    a.begin()
    conn.execute("INSERT INTO users (name) VALUES (?)", ("bob",))
    e = effect(0, "reader", {"name": "bob"})
    a.snapshot(e)
    out = a.apply(e, reader)
    assert out == {"rows": [[1, "bob"]], "ok": True}
