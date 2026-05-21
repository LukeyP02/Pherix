"""MySQLAdapter tests — mirror tests/test_adapters_sql.py against real MySQL.

Server-backed tests run against a live MySQL/MariaDB configured via the
``PHERIX_TEST_MYSQL_*`` environment variables (host/port/user/password/db). If
pymysql is not installed or no server is reachable, those tests skip cleanly —
so the suite stays green on a machine with no MySQL while still exercising the
real savepoint/apply/restore + version lane for anyone who has one.

Pure-helper tests (``_encode_key``, savepoint-name generation) need no server
and always run, so MySQL has non-skipped coverage even here where no MySQL
server is up.

Server-backed tests use a uniquely-named scratch table created inside the test
and dropped in teardown (try/finally) so reruns are clean.
"""

import os
import uuid

import pytest

from pherix.core.adapters.mysql import MySQLAdapter
from pherix.core.effects import Effect


# --- pure helpers: no driver, no server -------------------------------------


def test_name_is_mysql():
    assert MySQLAdapter.name == "mysql"


def test_encode_key_is_canonical():
    # List-coercion + sort_keys give a stable cross-process encoding.
    assert MySQLAdapter._encode_key(("users", 1)) == '["users", 1]'
    assert MySQLAdapter._encode_key(("a",)) == '["a"]'
    assert MySQLAdapter._encode_key(()) == "[]"


def test_encode_key_sorts_nested_dict():
    # sort_keys makes a nested dict's encoding order-independent — the property
    # that keeps the key canonical across processes.
    a = MySQLAdapter._encode_key(({"b": 2, "a": 1},))
    b = MySQLAdapter._encode_key(({"a": 1, "b": 2},))
    assert a == b


def test_savepoint_name_derives_from_index():
    assert MySQLAdapter._savepoint_name(0) == "sp_0"
    assert MySQLAdapter._savepoint_name(5) == "sp_5"


# --- server-backed tests ----------------------------------------------------

pymysql = pytest.importorskip("pymysql")

MYSQL_HOST = os.environ.get("PHERIX_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("PHERIX_TEST_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("PHERIX_TEST_MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("PHERIX_TEST_MYSQL_PASSWORD", "")
MYSQL_DB = os.environ.get("PHERIX_TEST_MYSQL_DB", "pherix_test")


def _mysql_conn():
    try:
        c = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DB,
        )
        c.autocommit(True)
        return c
    except Exception as e:  # noqa: BLE001 — any connect failure means "skip"
        pytest.skip(f"no reachable MySQL: {e}")


@pytest.fixture
def conn():
    c = _mysql_conn()
    yield c
    c.close()


@pytest.fixture
def scratch_table(conn):
    """A uniquely-named InnoDB (id, name) table, dropped in teardown."""
    table = f"pherix_t_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {table} "
            f"(id INT AUTO_INCREMENT PRIMARY KEY, name TEXT) ENGINE=InnoDB"
        )
    try:
        yield table
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")


def _insert(table):
    def insert_user(conn, name):
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {table} (name) VALUES (%s)", (name,))
        return name

    return insert_user


def _count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]


def effect(index, tool, args):
    return Effect(
        txn_id="t",
        index=index,
        tool=tool,
        args=args,
        resource="mysql",
        reversible=True,
    )


def test_supports_rollback(conn):
    assert MySQLAdapter(conn).supports_rollback() is True


def test_snapshot_apply_restore_round_trip(conn, scratch_table):
    a = MySQLAdapter(conn)
    a.begin()
    try:
        e = effect(0, "insert_user", {"name": "bob"})
        handle = a.snapshot(e)
        a.apply(e, _insert(scratch_table))
        assert _count(conn, scratch_table) == 1
        a.restore(handle)
        assert _count(conn, scratch_table) == 0
    finally:
        a.rollback()


def test_apply_returns_tool_result(conn, scratch_table):
    a = MySQLAdapter(conn)
    a.begin()
    try:
        e = effect(0, "insert_user", {"name": "bob"})
        a.snapshot(e)
        assert a.apply(e, _insert(scratch_table)) == "bob"
    finally:
        a.rollback()


def test_apply_injects_connection_as_first_arg(conn):
    seen = {}

    def spy(c, name):
        seen["conn"] = c

    a = MySQLAdapter(conn)
    a.begin()
    try:
        e = effect(0, "spy", {"name": "x"})
        a.snapshot(e)
        a.apply(e, spy)
        assert seen["conn"] is conn
    finally:
        a.rollback()


def test_restore_newest_first_unwinds_in_reverse(conn, scratch_table):
    a = MySQLAdapter(conn)
    a.begin()
    try:
        handles = []
        for i, name in enumerate(["a", "b", "c"]):
            e = effect(i, "insert_user", {"name": name})
            handles.append(a.snapshot(e))
            a.apply(e, _insert(scratch_table))
        assert _count(conn, scratch_table) == 3
        a.restore(handles[2])
        assert _count(conn, scratch_table) == 2
        a.restore(handles[1])
        assert _count(conn, scratch_table) == 1
        a.restore(handles[0])
        assert _count(conn, scratch_table) == 0
    finally:
        a.rollback()


def test_commit_persists_across_transactions(conn, scratch_table):
    a = MySQLAdapter(conn)
    a.begin()
    e = effect(0, "insert_user", {"name": "bob"})
    a.snapshot(e)
    a.apply(e, _insert(scratch_table))
    a.commit()
    a.begin()
    try:
        assert _count(conn, scratch_table) == 1
    finally:
        a.rollback()


def test_outer_rollback_discards_everything(conn, scratch_table):
    a = MySQLAdapter(conn)
    a.begin()
    for i, name in enumerate(["a", "b"]):
        e = effect(i, "insert_user", {"name": name})
        a.snapshot(e)
        a.apply(e, _insert(scratch_table))
    a.rollback()
    a.begin()
    try:
        assert _count(conn, scratch_table) == 0
    finally:
        a.rollback()


def test_apply_raises_then_snapshot_restorable(conn, scratch_table):
    """A tool error must leave the savepoint usable: prior row survives."""
    a = MySQLAdapter(conn)
    a.begin()
    try:
        e0 = effect(0, "insert_user", {"name": "good"})
        a.snapshot(e0)
        a.apply(e0, _insert(scratch_table))

        e1 = effect(1, "boom", {})
        handle = a.snapshot(e1)

        def boom(conn):
            with conn.cursor() as cur:
                cur.execute("INSERT INTO no_such_table_pherix VALUES (1)")

        with pytest.raises(Exception):  # noqa: PT011 — pymysql error type varies
            a.apply(e1, boom)

        a.restore(handle)
        assert _count(conn, scratch_table) == 1
    finally:
        a.rollback()


def test_read_version_absent_is_zero(conn):
    a = MySQLAdapter(conn)
    key = (f"k_{uuid.uuid4().hex}", 1)
    assert a.read_version(key) == 0


def test_write_version_monotonic(conn):
    a = MySQLAdapter(conn)
    key = (f"k_{uuid.uuid4().hex}", 1)
    try:
        assert a.write_version(key) == 1
        assert a.write_version(key) == 2
        assert a.write_version(key) == 3
        assert a.read_version(key) == 3
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM _pherix_versions WHERE resource = %s AND key_json = %s",
                (a.name, a._encode_key(key)),
            )


def test_distinct_keys_have_independent_versions(conn):
    a = MySQLAdapter(conn)
    k1 = (f"k_{uuid.uuid4().hex}", 1)
    k2 = (f"k_{uuid.uuid4().hex}", 2)
    try:
        a.write_version(k1)
        a.write_version(k1)
        a.write_version(k2)
        assert a.read_version(k1) == 2
        assert a.read_version(k2) == 1
    finally:
        with conn.cursor() as cur:
            for k in (k1, k2):
                cur.execute(
                    "DELETE FROM _pherix_versions "
                    "WHERE resource = %s AND key_json = %s",
                    (a.name, a._encode_key(k)),
                )
