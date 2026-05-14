import sqlite3

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
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
