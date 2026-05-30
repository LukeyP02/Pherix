"""Differential / metamorphic laws across backends.

The same effect sequence, folded through two different reversible adapters, must
yield equivalent journal semantics: the same committed world after commit, the
same restored world after rollback, and the same per-effect status sequence.
This is the metamorphic test — the *backend* is the thing that varies; the
journal algebra must not. We run the real :class:`SQLiteAdapter` against an
in-memory reference :class:`~tests._laws.DictAdapter` oracle.

A Postgres variant runs the same law against a *real* PostgreSQL via the
savepoint-backed :class:`PostgresAdapter`. It skips cleanly when psycopg is
absent or no server is reachable, so the offline, dependency-free kernel
invariant holds.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

from tests._laws import DictAdapter, dump_kv, fresh_kv_conn, kv_programs

# Trust pillar: audit — the differential facet: a program folds to an identical
# journal / committed world across two independent adapter implementations, so
# the recorded transcript is implementation-independent and trustworthy.
pytestmark = pytest.mark.audit

_LAW = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def both_backends_tools():
    """One logical toolset, one impl per backend (routed by resource)."""

    @tool(resource="sql", name="sql_set")
    def sql_set(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    @tool(resource="sql", name="sql_del")
    def sql_del(conn, k):
        conn.execute("DELETE FROM kv WHERE k = ?", (k,))

    @tool(resource="kv", name="dict_set")
    def dict_set(handle, k, v):
        handle.set(k, v)

    @tool(resource="kv", name="dict_del")
    def dict_del(handle, k):
        handle.delete(k)

    return {
        "sql": (sql_set, sql_del),
        "dict": (dict_set, dict_del),
    }


def _run_sql(conn, tools, prog, commit: bool):
    sql_set, sql_del = tools["sql"]
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        for op in prog:
            if op.op == "set":
                sql_set(k=op.key, v=op.value)
            else:
                sql_del(k=op.key)
        if not commit:
            txn.rollback()
    return txn


def _run_dict(adapter, tools, prog, commit: bool):
    dict_set, dict_del = tools["dict"]
    with agent_txn({"kv": adapter}) as txn:
        for op in prog:
            if op.op == "set":
                dict_set(k=op.key, v=op.value)
            else:
                dict_del(k=op.key)
        if not commit:
            txn.rollback()
    return txn


@given(prog=kv_programs())
@_LAW
def test_commit_equivalence_across_backends(both_backends_tools, prog):
    """After committing the same program, both backends hold the same world."""
    conn = fresh_kv_conn()
    try:
        sql_txn = _run_sql(conn, both_backends_tools, prog, commit=True)
        dict_adapter = DictAdapter()
        dict_txn = _run_dict(dict_adapter, both_backends_tools, prog, commit=True)

        assert dump_kv(conn) == dict_adapter.state()
        assert sql_txn.txn.state is TxnState.COMMITTED
        assert dict_txn.txn.state is TxnState.COMMITTED
        # Journal status sequences match step-for-step.
        assert [e.status for e in sql_txn.txn.effects] == [
            e.status for e in dict_txn.txn.effects
        ]
    finally:
        conn.close()


@given(prog=kv_programs())
@_LAW
def test_rollback_equivalence_across_backends(both_backends_tools, prog):
    """After rolling back the same program, both backends are empty again."""
    conn = fresh_kv_conn()
    try:
        _run_sql(conn, both_backends_tools, prog, commit=False)
        dict_adapter = DictAdapter()
        _run_dict(dict_adapter, both_backends_tools, prog, commit=False)

        assert dump_kv(conn) == {}
        assert dict_adapter.state() == {}
    finally:
        conn.close()


# --- Postgres variant -------------------------------------------------------
# The same metamorphic law against a *real* PostgreSQL, via the savepoint-backed
# PostgresAdapter. Skips cleanly when psycopg is absent or no server is
# reachable, so the offline, dependency-free kernel invariant holds. A live
# backend is exercised at a lighter example budget than the in-memory pair.

_PG_LAW = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def pg_kv():
    """A live-Postgres connection with a fresh ``kv`` table; skips offline."""
    import os

    psycopg = pytest.importorskip("psycopg", reason="Postgres driver not installed")
    dsn = os.environ.get("PHERIX_TEST_PG_DSN", "dbname=pherix_test")
    try:
        conn = psycopg.connect(dsn)
    except Exception as e:  # noqa: BLE001 — any connect failure means "skip"
        pytest.skip(f"no reachable Postgres: {e}")
    # The adapter drives BEGIN/COMMIT/ROLLBACK explicitly, so the raw connection
    # must be in autocommit (no implicit psycopg transaction wrapping ours).
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS kv")
        cur.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v INTEGER)")
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS kv")
        conn.close()


@pytest.fixture
def pg_tools():
    """The kv toolset in Postgres dialect (``%s`` params, ``EXCLUDED``)."""

    @tool(resource="sql", name="pg_set")
    def pg_set(conn, k, v):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv (k, v) VALUES (%s, %s) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                (k, v),
            )

    @tool(resource="sql", name="pg_del")
    def pg_del(conn, k):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kv WHERE k = %s", (k,))

    return pg_set, pg_del


def _dump_pg(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT k, v FROM kv")
        return dict(cur.fetchall())


def _run_pg(conn, pg_tools, prog, commit: bool):
    from pherix.core.adapters.postgres import PostgresAdapter

    pg_set, pg_del = pg_tools
    with conn.cursor() as cur:
        cur.execute("TRUNCATE kv")  # each example starts from an empty world
    with agent_txn({"sql": PostgresAdapter(conn)}) as txn:
        for op in prog:
            if op.op == "set":
                pg_set(k=op.key, v=op.value)
            else:
                pg_del(k=op.key)
        if not commit:
            txn.rollback()
    return txn


@given(prog=kv_programs())
@_PG_LAW
def test_pg_commit_equivalence_with_oracle(pg_kv, pg_tools, both_backends_tools, prog):
    """A committed program lands the same world in real Postgres and the oracle."""
    pg_txn = _run_pg(pg_kv, pg_tools, prog, commit=True)
    dict_adapter = DictAdapter()
    dict_txn = _run_dict(dict_adapter, both_backends_tools, prog, commit=True)

    assert _dump_pg(pg_kv) == dict_adapter.state()
    assert pg_txn.txn.state is TxnState.COMMITTED
    assert [e.status for e in pg_txn.txn.effects] == [
        e.status for e in dict_txn.txn.effects
    ]


@given(prog=kv_programs())
@_PG_LAW
def test_pg_rollback_equivalence_with_oracle(pg_kv, pg_tools, both_backends_tools, prog):
    """Rolling back the same program empties real Postgres, as it does the oracle."""
    _run_pg(pg_kv, pg_tools, prog, commit=False)
    dict_adapter = DictAdapter()
    _run_dict(dict_adapter, both_backends_tools, prog, commit=False)

    assert _dump_pg(pg_kv) == {}
    assert dict_adapter.state() == {}
