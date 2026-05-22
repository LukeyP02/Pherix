"""Private shared scaffolding for the cross-adapter conformance battery.

The leading underscore keeps pytest from collecting this module. It holds the
ONE place that knows how each backend is stood up offline (or skipped) and how
its "touched resource" is read back as a comparable value — so the law suites in
``test_conformance_adapters.py`` are a single parametrized matrix over every
adapter, and adding a tenth adapter is one entry here, not a new test file.

The shape of the abstraction
----------------------------
An adapter is a triple ``(snapshot, apply, restore)`` over a resource. To assert
the law *rollback ≈ identity* uniformly we need, per backend, three backend-
specific things and nothing else:

- a **factory** that yields a live ``(adapter, world)`` pair, set up against an
  in-process fake or a reachable real server, or skips cleanly when neither is
  present;
- a way to turn a logical *mutation* (insert / overwrite / delete / delete-
  absent) into the concrete tool the adapter's ``apply`` will run, plus the
  ``effect.args`` naming the touched key(s);
- a way to **read the whole touched resource back** as a plain comparable value
  (a dict / bytes) so "byte-identical after restore" is a literal ``==``.

Everything else — the snapshot/apply/restore fold, the version contract, the
isolation recording — is identical across backends and lives in the law suite,
parametrized over :data:`ADAPTER_CASES`.

The version contract (Slice 4) is split out because not every reversible adapter
implements it the same way: SQL-family adapters count (sentinel ``0``, monotonic
bump); content-addressed adapters (filesystem / memory) hash (sentinel
``"__missing__"``, recompute). :data:`VERSION_CASES` carries the per-family
sentinel + a key the adapter can version, so the version laws assert the right
contract per backend without special-casing inside the test body.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from pherix.core.effects import Effect


# ---------------------------------------------------------------------------
# A logical mutation, backend-independent.
#
# The four shapes the round-trip law must see (the same four ``_laws.kv_programs``
# exercises): writing a brand-new key, overwriting an existing one, deleting a
# present one, and deleting an absent one. Each backend's case translates these
# into its own tool body + args.
# ---------------------------------------------------------------------------

INSERT = "insert"        # write a key that did not exist
OVERWRITE = "overwrite"  # write a key that did exist (to a new value)
DELETE = "delete"        # delete a key that exists
DELETE_ABSENT = "delete_absent"  # delete a key that does not exist

ALL_MUTATIONS = [INSERT, OVERWRITE, DELETE, DELETE_ABSENT]


@dataclass
class AdapterCase:
    """One backend wired for the conformance battery.

    ``name`` is the pytest id. ``supports_rollback`` mirrors the adapter's
    honesty flag; reversible cases run the round-trip law, irreversible ones run
    the irreversibility law instead. The callables are filled per backend:

    - ``seed(world, key, value)`` puts a key into a known starting state.
    - ``tool_for(mutation)`` returns the ``tool_fn`` ``adapter.apply`` will call.
    - ``args_for(mutation, key, value)`` is the ``effect.args`` naming the
      touched key(s) — the snapshot lane reads keys off these for the
      route-b adapters (redis/s3/mongo), and SQL-family tools take them as
      kwargs.
    - ``dump(world)`` reads the *whole touched namespace* back as a comparable
      plain value.
    """

    name: str
    supports_rollback: bool
    factory: Callable[[], Any]  # context-manager-like: yields (adapter, world)
    seed: Callable[[Any, str, Any], None] = None
    tool_for: Callable[[str], Callable[..., Any]] = None
    args_for: Callable[[str, str, Any], dict] = None
    dump: Callable[[Any], Any] = None


@dataclass
class VersionCase:
    """One backend's version contract, for the version-semantics law.

    ``missing`` is the non-None sentinel ``read_version`` returns for an absent
    key (``0`` for SQL counters, ``"__missing__"`` for content hashes).
    ``family`` is ``"counter"`` or ``"hash"`` — the law asserts monotonic-bump
    for counters and recompute-on-content for hashes. ``write(world, key,
    value)`` performs a real write the adapter can then version (content-
    addressed adapters need actual bytes on disk to hash).
    """

    name: str
    family: str
    missing: Any
    factory: Callable[[], Any]
    make_key: Callable[[], tuple]
    write: Callable[[Any, Any, tuple, Any], None] = None  # (adapter, world, key, value)


def _effect(resource: str, args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="conf",
        index=index,
        tool="conf_tool",
        args=args,
        resource=resource,
        reversible=True,
    )


# ===========================================================================
# Per-backend wiring. Each ``_case_*`` is a generator used as a context manager
# (``with contextlib.contextmanager``-style via the law's ``yield``-driven
# fixture) — but to keep imports light we hand the law a zero-arg factory that
# returns an object exposing ``__enter__``/``__exit__``. We use plain
# ``contextlib.contextmanager`` for that.
# ===========================================================================

import contextlib


# --- SQLite (always runs) ---------------------------------------------------
#
# A real :memory: SQLite + the production SQLiteAdapter, so the round-trip law
# folds the actual SAVEPOINT machinery, not a toy. World is the kv table dict.


@contextlib.contextmanager
def _sqlite_world():
    from pherix.core.adapters.sql import SQLiteAdapter

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    adapter = SQLiteAdapter(conn)
    adapter.begin()
    try:
        yield adapter, conn
    finally:
        try:
            adapter.rollback()
        except Exception:
            pass
        conn.close()


def _kv_dump(conn) -> dict:
    return {k: v for k, v in conn.execute("SELECT k, v FROM kv")}


def _sql_seed(conn, key, value):
    conn.execute(
        "INSERT INTO kv (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, str(value)),
    )


def _sql_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(conn, key, value):
            conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, str(value)),
            )
        return tool

    def tool(conn, key, value):
        conn.execute("DELETE FROM kv WHERE k = ?", (key,))
    return tool


def _kv_args(mutation, key, value):
    return {"key": key, "value": value}


# --- Memory (always runs) ---------------------------------------------------
#
# MemoryAdapter over a :memory: SQLite. Mutations go through MemoryHandle's
# remember/forget so the real handle code path is exercised.


@contextlib.contextmanager
def _memory_world():
    from pherix.core.adapters.memory import MemoryAdapter

    conn = sqlite3.connect(":memory:", isolation_level=None)
    adapter = MemoryAdapter(conn, namespace="conf")
    adapter.begin()
    try:
        yield adapter, conn
    finally:
        try:
            adapter.rollback()
        except Exception:
            pass
        conn.close()


def _memory_dump(conn) -> dict:
    return {
        k: v
        for k, v in conn.execute(
            "SELECT mem_key, value FROM _pherix_memory WHERE namespace = ?",
            ("conf",),
        )
    }


def _memory_seed(conn, key, value):
    conn.execute(
        "INSERT INTO _pherix_memory (namespace, mem_key, value, ts) "
        "VALUES (?, ?, ?, '') "
        "ON CONFLICT(namespace, mem_key) DO UPDATE SET value = excluded.value",
        ("conf", key, str(value)),
    )


def _memory_tool_for(mutation):
    # apply() injects a MemoryHandle as the first arg.
    if mutation in (INSERT, OVERWRITE):
        def tool(handle, key, value):
            handle.remember(key, str(value))
        return tool

    def tool(handle, key, value):
        handle.forget(key)
    return tool


# --- Filesystem (always runs) -----------------------------------------------
#
# FilesystemAdapter over a real tempdir. World is {relpath: bytes}.


@contextlib.contextmanager
def _fs_world():
    from pherix.core.adapters.filesystem import FilesystemAdapter

    with tempfile.TemporaryDirectory() as root:
        adapter = FilesystemAdapter(root)
        adapter.begin()
        try:
            yield adapter, Path(root)
        finally:
            try:
                adapter.commit()
            except Exception:
                pass


def _fs_dump(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _fs_seed(root: Path, key, value):
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(str(value).encode())


def _fs_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(handle, key, value):
            handle.write(key, str(value).encode())
        return tool

    def tool(handle, key, value):
        handle.delete(key)
    return tool


# --- Redis (skips without fakeredis) ----------------------------------------


@contextlib.contextmanager
def _redis_world():
    fakeredis = pytest.importorskip("fakeredis")
    from pherix.core.adapters.redis import RedisAdapter

    client = fakeredis.FakeStrictRedis()
    yield RedisAdapter(client), client


def _redis_dump(client) -> dict:
    out = {}
    for k in client.keys("*"):
        name = k.decode() if isinstance(k, bytes) else k
        out[name] = client.get(k)
    return out


def _redis_seed(client, key, value):
    client.set(key, str(value).encode())


def _redis_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(client, key, value):
            client.set(key, str(value).encode())
        return tool

    def tool(client, key, value):
        client.delete(key)
    return tool


# --- S3 (skips without moto + boto3) ----------------------------------------

_S3_BUCKET = "pherix-conf-bucket"


@contextlib.contextmanager
def _s3_world():
    pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    from moto import mock_aws

    from pherix.core.adapters.s3 import S3Adapter

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_S3_BUCKET)
        yield S3Adapter(client, _S3_BUCKET), client


def _s3_dump(client) -> dict:
    out = {}
    resp = client.list_objects_v2(Bucket=_S3_BUCKET)
    for obj in resp.get("Contents", []):
        body = client.get_object(Bucket=_S3_BUCKET, Key=obj["Key"])["Body"].read()
        out[obj["Key"]] = body
    return out


def _s3_seed(client, key, value):
    client.put_object(Bucket=_S3_BUCKET, Key=key, Body=str(value).encode())


def _s3_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(client, key, value):
            client.put_object(Bucket=_S3_BUCKET, Key=key, Body=str(value).encode())
        return tool

    def tool(client, key, value):
        client.delete_object(Bucket=_S3_BUCKET, Key=key)
    return tool


# --- MongoDB (skips without mongomock) --------------------------------------
#
# Mongo addresses documents by (collection, doc_id), so the conformance key
# maps to a fixed collection + the key string as doc_id. The "value" lives in a
# field; dump reads {doc_id: value}.

_MONGO_COLL = "conf"


@contextlib.contextmanager
def _mongo_world():
    mongomock = pytest.importorskip("mongomock")
    from pherix.core.adapters.mongodb import MongoAdapter

    db = mongomock.MongoClient().confdb
    yield MongoAdapter(db), db


def _mongo_dump(db) -> dict:
    return {d["_id"]: d.get("v") for d in db[_MONGO_COLL].find({})}


def _mongo_seed(db, key, value):
    db[_MONGO_COLL].replace_one(
        {"_id": key}, {"_id": key, "v": str(value)}, upsert=True
    )


def _mongo_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(database, collection, doc_id, value):
            database[collection].replace_one(
                {"_id": doc_id}, {"_id": doc_id, "v": str(value)}, upsert=True
            )
        return tool

    def tool(database, collection, doc_id, value):
        database[collection].delete_one({"_id": doc_id})
    return tool


def _mongo_args(mutation, key, value):
    return {"collection": _MONGO_COLL, "doc_id": key, "value": value}


# --- Postgres (skips without a reachable server) ----------------------------
#
# No in-process fake driver exists for Postgres, so this case stands up a real
# connection and SKIPS cleanly if psycopg is absent or no server is reachable —
# mirroring tests/test_adapters_postgres.py. Where a server IS present (CI with
# PG, or a dev box) the full round-trip runs against real savepoints. A
# uniquely-named scratch table is created per case and dropped in teardown so
# reruns are clean even after a crash mid-test.


@contextlib.contextmanager
def _postgres_world():
    psycopg = pytest.importorskip("psycopg")
    from pherix.core.adapters.postgres import PostgresAdapter

    dsn = os.environ.get("PHERIX_TEST_PG_DSN", "dbname=pherix_test")
    try:
        conn = psycopg.connect(dsn)
        conn.autocommit = True
    except Exception as e:  # noqa: BLE001 — any connect failure means skip
        pytest.skip(f"no reachable Postgres: {e}")

    table = f"pherix_conf_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    adapter = PostgresAdapter(conn)
    adapter.begin()
    try:
        yield adapter, (conn, table)
    finally:
        try:
            adapter.rollback()
        except Exception:
            pass
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        conn.close()


def _pg_dump(world) -> dict:
    conn, table = world
    with conn.cursor() as cur:
        cur.execute(f"SELECT k, v FROM {table}")
        return {k: v for k, v in cur.fetchall()}


def _pg_seed(world, key, value):
    conn, table = world
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table} (k, v) VALUES (%s, %s) "
            f"ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
            (key, str(value)),
        )


def _pg_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(world, key, value):
            conn, table = world
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} (k, v) VALUES (%s, %s) "
                    f"ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                    (key, str(value)),
                )
        return tool

    def tool(world, key, value):
        conn, table = world
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE k = %s", (key,))
    return tool


# Postgres' apply injects the bare connection as the first arg, but our dump /
# tools need (conn, table). The adapter's apply does ``tool_fn(self._conn,
# **args)`` — so we cannot pass the table through apply. Instead the tool closes
# over the table via a per-case binding done in the law (it has the world).
# To keep apply's contract, we wrap: see ``pg_apply_tool`` in the law.


# --- MySQL (skips without a reachable server) -------------------------------
#
# Identical shape to Postgres: no in-process fake driver exists, so this case
# stands up a real pymysql connection and SKIPS cleanly if pymysql is absent or
# no server is reachable — mirroring tests/test_adapters_mysql.py. Listed in the
# matrix so all 9 adapters appear; it runs against real savepoints wherever a
# MySQL/MariaDB is configured via the ``PHERIX_TEST_MYSQL_*`` env vars. MySQL
# uses ``ON DUPLICATE KEY UPDATE`` (vs Postgres' ``ON CONFLICT``) and a length-
# bounded ``VARCHAR`` primary key (TEXT cannot be a PK in InnoDB without one).


@contextlib.contextmanager
def _mysql_world():
    pymysql = pytest.importorskip("pymysql")
    from pherix.core.adapters.mysql import MySQLAdapter

    try:
        conn = pymysql.connect(
            host=os.environ.get("PHERIX_TEST_MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("PHERIX_TEST_MYSQL_PORT", "3306")),
            user=os.environ.get("PHERIX_TEST_MYSQL_USER", "root"),
            password=os.environ.get("PHERIX_TEST_MYSQL_PASSWORD", ""),
            database=os.environ.get("PHERIX_TEST_MYSQL_DB", "pherix_test"),
        )
        conn.autocommit(True)
    except Exception as e:  # noqa: BLE001 — any connect failure means skip
        pytest.skip(f"no reachable MySQL: {e}")

    table = f"pherix_conf_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {table} "
            f"(k VARCHAR(255) PRIMARY KEY, v TEXT NOT NULL) ENGINE=InnoDB"
        )
    adapter = MySQLAdapter(conn)
    adapter.begin()
    try:
        yield adapter, (conn, table)
    finally:
        try:
            adapter.rollback()
        except Exception:
            pass
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        conn.close()


def _my_dump(world) -> dict:
    conn, table = world
    with conn.cursor() as cur:
        cur.execute(f"SELECT k, v FROM {table}")
        return {k: v for k, v in cur.fetchall()}


def _my_seed(world, key, value):
    conn, table = world
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table} (k, v) VALUES (%s, %s) "
            f"ON DUPLICATE KEY UPDATE v = VALUES(v)",
            (key, str(value)),
        )


def _my_tool_for(mutation):
    if mutation in (INSERT, OVERWRITE):
        def tool(world, key, value):
            conn, table = world
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} (k, v) VALUES (%s, %s) "
                    f"ON DUPLICATE KEY UPDATE v = VALUES(v)",
                    (key, str(value)),
                )
        return tool

    def tool(world, key, value):
        conn, table = world
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE k = %s", (key,))
    return tool


def _my_version_write(adapter, world, key, value):
    adapter.write_version(key)


# --- HTTP (always runs — irreversible) --------------------------------------
#
# The irreversibility law: supports_rollback() is False, snapshot/restore both
# raise, and an effect routes down the staging + gate lane through the runtime.


@contextlib.contextmanager
def _http_world():
    from pherix.core.adapters.http import HTTPAdapter

    yield HTTPAdapter(), None


# ===========================================================================
# The registries the law modules iterate.
# ===========================================================================

# Reversible adapters that take a single named key (`args["key"]`) and whose
# tool signature is ``(handle/conn/client, key, value)``. SQLite, Memory,
# Filesystem, Redis, S3 share this shape.
_KEYED_REVERSIBLE = [
    AdapterCase(
        name="sqlite",
        supports_rollback=True,
        factory=_sqlite_world,
        seed=_sql_seed,
        tool_for=_sql_tool_for,
        args_for=_kv_args,
        dump=_kv_dump,
    ),
    AdapterCase(
        name="memory",
        supports_rollback=True,
        factory=_memory_world,
        seed=_memory_seed,
        tool_for=_memory_tool_for,
        args_for=_kv_args,
        dump=_memory_dump,
    ),
    AdapterCase(
        name="filesystem",
        supports_rollback=True,
        factory=_fs_world,
        seed=_fs_seed,
        tool_for=_fs_tool_for,
        args_for=_kv_args,
        dump=_fs_dump,
    ),
    AdapterCase(
        name="redis",
        supports_rollback=True,
        factory=_redis_world,
        seed=_redis_seed,
        tool_for=_redis_tool_for,
        args_for=_kv_args,
        dump=_redis_dump,
    ),
    AdapterCase(
        name="s3",
        supports_rollback=True,
        factory=_s3_world,
        seed=_s3_seed,
        tool_for=_s3_tool_for,
        args_for=_kv_args,
        dump=_s3_dump,
    ),
]

# Mongo's tool signature + args differ (collection/doc_id), so it is its own
# entry but folds through the identical law body.
_MONGO_CASE = AdapterCase(
    name="mongodb",
    supports_rollback=True,
    factory=_mongo_world,
    seed=_mongo_seed,
    tool_for=_mongo_tool_for,
    args_for=_mongo_args,
    dump=_mongo_dump,
)

# Postgres' apply injects only the bare connection, so the tool cannot receive
# the scratch-table name through apply. It is wired specially in the law via a
# closure; the case here carries the factory + dump + seed but the law builds
# the tool per-world. We still list it so it appears in the matrix (and skips
# cleanly when no server is reachable).
_POSTGRES_CASE = AdapterCase(
    name="postgres",
    supports_rollback=True,
    factory=_postgres_world,
    seed=_pg_seed,
    tool_for=_pg_tool_for,
    args_for=_kv_args,
    dump=_pg_dump,
)

# MySQL mirrors Postgres exactly (bare-connection injection, wired in the law);
# listed so the matrix carries all 9 adapters, skipping cleanly with no server.
_MYSQL_CASE = AdapterCase(
    name="mysql",
    supports_rollback=True,
    factory=_mysql_world,
    seed=_my_seed,
    tool_for=_my_tool_for,
    args_for=_kv_args,
    dump=_my_dump,
)

REVERSIBLE_CASES = _KEYED_REVERSIBLE + [_MONGO_CASE, _POSTGRES_CASE, _MYSQL_CASE]

IRREVERSIBLE_CASES = [
    AdapterCase(
        name="http",
        supports_rollback=False,
        factory=_http_world,
    ),
]

ALL_CASES = REVERSIBLE_CASES + IRREVERSIBLE_CASES


# ---------------------------------------------------------------------------
# Version-contract registry. Split by family so the law asserts the right
# sentinel + behaviour per backend, gating only on adapters that implement it.
# ---------------------------------------------------------------------------


def _sql_version_write(adapter, world, key, value):
    # SQL counters bump via write_version directly (no underlying row needed —
    # the side-table is the version store).
    adapter.write_version(key)


def _fs_version_write(adapter, world, key, value):
    # Content-addressed: an actual file must exist for read_version to hash.
    root = world
    target = root / key[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(str(value).encode())


def _memory_version_write(adapter, world, key, value):
    conn = world
    conn.execute(
        "INSERT INTO _pherix_memory (namespace, mem_key, value, ts) "
        "VALUES (?, ?, ?, '') "
        "ON CONFLICT(namespace, mem_key) DO UPDATE SET value = excluded.value",
        ("conf", key[0], str(value)),
    )


def _pg_version_write(adapter, world, key, value):
    adapter.write_version(key)


VERSION_CASES = [
    VersionCase(
        name="sqlite",
        family="counter",
        missing=0,
        factory=_sqlite_world,
        make_key=lambda: ("kv", uuid.uuid4().hex),
        write=_sql_version_write,
    ),
    VersionCase(
        name="memory",
        family="hash",
        missing="__missing__",
        factory=_memory_world,
        make_key=lambda: (uuid.uuid4().hex,),
        write=_memory_version_write,
    ),
    VersionCase(
        name="filesystem",
        family="hash",
        missing="__missing__",
        factory=_fs_world,
        make_key=lambda: (f"{uuid.uuid4().hex}.bin",),
        write=_fs_version_write,
    ),
    VersionCase(
        name="postgres",
        family="counter",
        missing=0,
        factory=_postgres_world,
        make_key=lambda: ("kv", uuid.uuid4().hex),
        write=_pg_version_write,
    ),
    VersionCase(
        name="mysql",
        family="counter",
        missing=0,
        factory=_mysql_world,
        make_key=lambda: ("kv", uuid.uuid4().hex),
        write=_my_version_write,
    ),
]
