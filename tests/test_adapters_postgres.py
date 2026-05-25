"""PostgresAdapter tests — mirror tests/test_adapters_sql.py against real PG.

These run against a live PostgreSQL: a connection to ``PHERIX_TEST_PG_DSN``
(default ``dbname=pherix_test``). If psycopg is not installed or no Postgres is
reachable, the whole module skips cleanly — so the suite stays green on a
machine with no PG, while actually exercising savepoint/apply/restore + the
version side-table where PG is present.

Every test that mutates uses a uniquely-named scratch table created inside the
test and dropped in teardown (try/finally) so reruns are clean even after a
crash mid-test.
"""

import os
import uuid

import pytest

from pherix.core.adapters.postgres import PostgresAdapter
from pherix.core.effects import Effect

psycopg = pytest.importorskip("psycopg")

PG_DSN = os.environ.get("PHERIX_TEST_PG_DSN", "dbname=pherix_test")


def _pg_conn():
    try:
        c = psycopg.connect(PG_DSN)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001 — any connect failure means "skip"
        pytest.skip(f"no reachable Postgres: {e}")


@pytest.fixture
def conn():
    c = _pg_conn()
    yield c
    c.close()


@pytest.fixture
def scratch_table(conn):
    """A uniquely-named (id SERIAL, name TEXT) table, dropped in teardown."""
    table = f"pherix_t_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (id SERIAL PRIMARY KEY, name TEXT)")
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
        resource="postgres",
        reversible=True,
    )


# --- name / honesty contract (no server state needed beyond connect) -------


def test_name_is_postgres(conn):
    assert PostgresAdapter(conn).name == "postgres"


def test_supports_rollback(conn):
    assert PostgresAdapter(conn).supports_rollback() is True


def test_savepoint_name_derives_from_effect_index(conn):
    a = PostgresAdapter(conn)
    a.begin()
    try:
        h = a.snapshot(effect(5, "x", {"name": "bob"}))
        assert h.payload["savepoint"] == "sp_5"
    finally:
        a.rollback()


# --- golden round-trip: savepoint -> insert -> ROLLBACK TO SAVEPOINT -------


def test_snapshot_apply_restore_round_trip(conn, scratch_table):
    a = PostgresAdapter(conn)
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
    a = PostgresAdapter(conn)
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

    a = PostgresAdapter(conn)
    a.begin()
    try:
        e = effect(0, "spy", {"name": "x"})
        a.snapshot(e)
        a.apply(e, spy)
        assert seen["conn"] is conn
    finally:
        a.rollback()


def test_restore_newest_first_unwinds_in_reverse(conn, scratch_table):
    a = PostgresAdapter(conn)
    a.begin()
    try:
        handles = []
        for i, name in enumerate(["a", "b", "c"]):
            e = effect(i, "insert_user", {"name": name})
            handles.append(a.snapshot(e))
            a.apply(e, _insert(scratch_table))
        assert _count(conn, scratch_table) == 3
        # backward fold: restore the newest savepoint first
        a.restore(handles[2])
        assert _count(conn, scratch_table) == 2
        a.restore(handles[1])
        assert _count(conn, scratch_table) == 1
        a.restore(handles[0])
        assert _count(conn, scratch_table) == 0
    finally:
        a.rollback()


def test_commit_persists_across_transactions(conn, scratch_table):
    a = PostgresAdapter(conn)
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
    a = PostgresAdapter(conn)
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


# --- failure path: apply raises, snapshot still restorable -----------------


def test_apply_raises_then_snapshot_restorable(conn, scratch_table):
    """A tool error must leave the savepoint usable.

    Postgres aborts the *current statement* on error but, with an enclosing
    SAVEPOINT, ``ROLLBACK TO SAVEPOINT`` rewinds the txn to a clean point —
    this is exactly the property the runtime relies on to undo a half-applied
    effect. We assert the prior row survives the rollback-to-savepoint.
    """
    a = PostgresAdapter(conn)
    a.begin()
    try:
        # One good row first.
        e0 = effect(0, "insert_user", {"name": "good"})
        a.snapshot(e0)
        a.apply(e0, _insert(scratch_table))

        # Snapshot, then a tool that raises a real DB error (bad SQL).
        e1 = effect(1, "boom", {})
        handle = a.snapshot(e1)

        def boom(conn):
            with conn.cursor() as cur:
                cur.execute("INSERT INTO no_such_table_pherix VALUES (1)")

        with pytest.raises(Exception):  # noqa: PT011 — psycopg error type varies
            a.apply(e1, boom)

        # Without a rollback-to-savepoint the txn is in an aborted state;
        # restore rewinds to the clean savepoint and the txn is usable again.
        a.restore(handle)
        assert _count(conn, scratch_table) == 1
    finally:
        a.rollback()


# --- versioning (Slice 4) ---------------------------------------------------


def test_read_version_absent_is_zero(conn):
    a = PostgresAdapter(conn)
    key = (f"k_{uuid.uuid4().hex}", 1)
    assert a.read_version(key) == 0


def test_write_version_monotonic(conn):
    a = PostgresAdapter(conn)
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
    a = PostgresAdapter(conn)
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


def test_encode_key_is_canonical():
    # Pure helper — no server needed. List-coercion + sort_keys give a stable
    # cross-process encoding.
    assert PostgresAdapter._encode_key(("users", 1)) == '["users", 1]'
    assert PostgresAdapter._encode_key(("a",)) == '["a"]'
