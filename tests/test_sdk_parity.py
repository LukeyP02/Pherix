"""SDK-level Py<->TS parity: same scenario, both engines, identical journals.

This is the proof that ``pherix`` (Python) and ``pherix-ts`` (the Node SDK) are
*one system* and not two implementations that happen to look alike. It is
DISTINCT from ``tests/test_governance_js_conformance.py`` — that test only
checks the *browser* policy-verdict mirror (``site/policy-eval.js``) against the
Python policy preview. This one drives the actual ``pherix-ts`` SDK
(``agentTxn`` / ``tool`` / the sql, fs, http adapters) under Node AND the
Python ``agent_txn`` in-process, then DIFFS the resulting effect journals.

How it works
------------
Each scenario is defined ONCE conceptually and implemented twice — a Python
runner (a closure in ``SCENARIOS`` below) and a TS runner (a branch in
``pherix-ts/test/parity/runner.mts``, keyed by the same scenario name). Both
emit the journal in the SAME canonical shape (:func:`_canonical_journal`):

    {
      "final_state": "<TxnState.value>",          # e.g. "committed"
      "outcome":     "<ok | GateBlocked | IsolationConflict>",
      "effects": [
        {"index", "tool", "resource", "reversible", "status",
         "read_keys": [[resource, key, version], ...],
         "write_keys": [[resource, key, version_after], ...]}, ...
      ]
    }

The Python test runs its closure in-process, shells out to Node for the TS
journal, and asserts STRUCTURAL equality: same effect order, same status
transitions (the terminal status per effect), same read/write keys, same final
transaction state and outcome.

What is normalized OUT (and why)
--------------------------------
Some fields legitimately differ by language or vary per run; comparing them
would produce false negatives that are not parity bugs. We assert on STRUCTURE,
not on these values:

* ``txn_id`` — random per run (a uuid). Never a parity claim.
* ``effect_id`` — a content hash of (txn_id, index, tool, args). The *inputs*
  that feed it (tool, index, ordering) ARE asserted directly, so the hash is
  redundant to compare; comparing it would only risk a cross-language
  hashing-detail false negative that says nothing about journal structure.
  (Both languages already pin the hash byte-for-byte in their own effect-id
  tests; this suite need not re-litigate that.)
* ``ts`` / timestamps — wall clock.
* ``result`` / ``snapshot`` payloads — driver-specific objects (a SAVEPOINT
  name, a better-sqlite3 run-info, a Python sqlite3 cursor). The STATUS
  transition (APPLIED / STAGED / GATED / COMPENSATED) already captures which
  lane the effect went down, which is the structural fact parity is about.
* ``args`` — scenario-internal values (an amount, an email body). Asserted
  indirectly via tool name + journal order; the literal values are not a
  cross-SDK contract.

The enum string VALUES are identical across both SDKs by construction —
``TxnState`` and ``EffectStatus`` share the same wire strings (``"committed"``,
``"applied"``, ``"gated"``, ...) — so they are compared directly with no
language-specific mapping. That shared vocabulary is itself part of what makes
the two SDKs one system; if it ever drifts, this suite catches it.

Skip mechanism
--------------
The journal-diff test (:func:`test_sdk_journal_parity`) is ``skipif(node is
None)`` so the offline Python suite stays green on a machine without Node. The
Node runner is run from source via ``npx tsx`` (already a ``pherix-ts``
devDependency) — no build step. If ``node_modules`` is absent the subprocess
fails and the test reports it loudly rather than silently passing.

The coverage GATES are pure Python and run unconditionally — static contract
checks that need no Node, so they hold even on a Node-less machine (exactly
where the journal-diff test, ``skipif(NODE is None)``, cannot help):

* :func:`test_parity_scenarios_cover_every_adapter` — every shipped *Python*
  adapter has a parity scenario.
* :func:`test_adapter_inventory_symmetric_across_sdks` — the Python and TS
  adapter inventories are identical, so neither SDK can ship an adapter the
  other lacks without failing loudly. The earlier gate only walks the Python
  adapters, so a TS-only adapter (or a Python adapter never ported to TS) would
  otherwise slip past every Node-less run.
* :func:`test_parity_scenarios_symmetric_across_sdks` — the Python ``SCENARIOS``
  list and the TS runner's ``SCENARIOS`` map name the same scenarios, so a
  scenario added on one side without its twin fails even without Node.

Extending per adapter
---------------------
The orchestrator will add one scenario per newly-ported TS adapter (git, s3,
redis, mongodb, mysql, dynamodb, gcs, elasticsearch, rest, messagequeue) once
T1/T2 land. To add one: append a ``Scenario`` to :data:`SCENARIOS` here with a
Python runner closure, and add a matching branch under the SAME name to
``SCENARIOS`` in ``pherix-ts/test/parity/runner.mts``. No other wiring — the
parametrized test picks it up automatically.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from pherix.core.adapters.dynamodb import DynamoDBAdapter
from pherix.core.adapters.elasticsearch import ElasticsearchAdapter
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.gcs import GCSAdapter
from pherix.core.adapters.git import GitAdapter
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.memory import MemoryAdapter
from pherix.core.adapters.messagequeue import MQAdapter, publish_tool
from pherix.core.adapters.mongodb import MongoAdapter
from pherix.core.adapters.mysql import MySQLAdapter
from pherix.core.adapters.postgres import PostgresAdapter
from pherix.core.adapters.redis import RedisAdapter
from pherix.core.adapters.rest import RESTAdapter, rest_tool
from pherix.core.adapters.s3 import S3Adapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.isolation import IsolationConflict
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import REGISTRY, tool
from pherix.core.transaction import TxnState

NODE = shutil.which("node")
NPX = shutil.which("npx")
PHERIX_TS = Path(__file__).resolve().parent.parent / "pherix-ts"
RUNNER = PHERIX_TS / "test" / "parity" / "runner.mts"
TS_ADAPTERS_DIR = PHERIX_TS / "src" / "adapters"


# -- canonicalization --------------------------------------------------------


def _canonical_journal(effects: list[Any], final_state: TxnState, outcome: str) -> dict:
    """Reduce a Python txn's journal to the language-agnostic comparable shape.

    Keys are emitted as lists-of-lists so they round-trip through JSON the same
    way the TS side's tuple-shaped arrays do. The Python ``read_keys`` entries
    are ``(resource, tuple(key), version)``; ``tuple(key)`` JSON-serialises to a
    list, matching the TS ``[resource, [..key..], version]`` exactly.
    """
    return {
        "final_state": final_state.value,
        "outcome": outcome,
        "effects": [
            {
                "index": e.index,
                "tool": e.tool,
                "resource": e.resource,
                "reversible": e.reversible,
                "status": e.status.value,
                # json round-trip normalizes tuples -> lists so the structural
                # comparison matches the TS arrays. We compare the round-tripped
                # form on BOTH sides (see _run_python) to keep it apples-to-apples.
                "read_keys": e.read_keys,
                "write_keys": e.write_keys,
            }
            for e in effects
        ],
    }


def _json_normalized(obj: Any) -> Any:
    """Round-trip through JSON so tuples become lists, matching the TS shape.

    Applied to BOTH the Python and the Node journal before comparison, so the
    only thing being asserted is structural identity, not Python-vs-JSON
    container quirks (tuple vs list).
    """
    return json.loads(json.dumps(obj))


# -- the scenarios (Python half) ---------------------------------------------
# Each runner builds fresh resources + tools, runs the scenario through the
# real ``agent_txn``, and returns (effects, final_state, outcome). The matching
# TS half lives under the same name in pherix-ts/test/parity/runner.mts.


def _reversible_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: a SQLite write that commits. STAGED default -> APPLIED."""
    REGISTRY.clear()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)")
    conn.execute("INSERT INTO accounts VALUES (?, ?)", ("alice", 100))
    conn.execute("INSERT INTO accounts VALUES (?, ?)", ("bob", 0))
    sql = SQLiteAdapter(conn)

    @tool("sql", name="transfer")
    def transfer(conn, from_: str, to: str, amount: int):  # noqa: ANN001
        conn.execute("UPDATE accounts SET balance = balance - ? WHERE name = ?", (amount, from_))
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE name = ?", (amount, to))
        return {"ok": True}

    with agent_txn({"sql": sql}) as ctx:
        transfer(from_="alice", to="bob", amount=30)
    return list(ctx.txn.effects), ctx.txn.state, "ok"


def _irreversible_gate() -> tuple[list[Any], TxnState, str]:
    """Irreversible + gate: HTTP effect, no compensator. commit() gates ->
    ROLLED_BACK, effect GATED. The external side-effect never fires."""
    REGISTRY.clear()
    http = HTTPAdapter()
    fired: list[str] = []

    @tool("http", reversible=False, injects_handle=False, name="sendEmail")
    def send_email(to: str, body: str):  # noqa: ANN001
        fired.append(to)
        return {"delivered": True}

    outcome = "ok"
    captured: list[Any] = []
    try:
        with agent_txn({"http": http}) as ctx:
            captured.append(ctx)
            send_email(to="user@example.com", body="hello")
    except GateBlocked:
        outcome = "GateBlocked"
    ctx = captured[0]
    assert not fired, "gate must stop the irreversible effect from firing"
    return list(ctx.txn.effects), ctx.txn.state, outcome


def _isolation_conflict() -> tuple[list[Any], TxnState, str]:
    """Isolation conflict: a read key written before commit (lost update).
    commit() raises IsolationConflict; the txn unwinds to ROLLED_BACK."""
    REGISTRY.clear()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)")
    conn.execute("INSERT INTO accounts VALUES (?, ?)", ("alice", 100))
    sql = SQLiteAdapter(conn)

    @tool("sql", name="readBalance")
    def read_balance(conn, name: str):  # noqa: ANN001
        cur = execute_isolated(
            conn,
            "SELECT balance FROM accounts WHERE name = ?",
            (name,),
            reads=[("accounts", name)],
        )
        # Return a JSON-serialisable value (the row), not the raw cursor — the
        # cursor would choke the audit journal's effect-result serialisation,
        # and the TS executeIsolated already returns rows (.all()) for a reader.
        row = cur.fetchone()
        return {"balance": row[0] if row else None}

    outcome = "ok"
    captured: list[Any] = []
    try:
        with agent_txn({"sql": sql}) as ctx:
            captured.append(ctx)
            read_balance(name="alice")  # records read of alice at version 0
            # Simulate a concurrent committed write bumping alice's version.
            sql.write_version(("accounts", "alice"))
    except IsolationConflict:
        outcome = "IsolationConflict"
    ctx = captured[0]
    return list(ctx.txn.effects), ctx.txn.state, outcome


# -- per-adapter scenarios (one per newly-ported TS adapter) -----------------
# Each reversible adapter gets ONE commit scenario: a single tool call that
# touches one key against an in-memory fake. The effect goes STAGED -> APPLIED
# and the txn commits. The journal only captures resource/reversible/status/
# read/write-keys (not backend bytes), so the fake just has to route the effect
# down the same lane as its TS twin. read_keys/write_keys are empty on both
# sides (these tools record none) — the parity claim is the identical resource
# name + reversible flag + status lane. Each irreversible adapter gets ONE gate
# scenario, mirroring ``_irreversible_gate``.


class _FakeS3:
    """In-memory S3: snapshot's get_object raises NoSuchKey for an absent key."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 — boto3 casing
        if Key not in self.store:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self.store[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes):  # noqa: N803
        self.store[Key] = Body
        return {}

    def delete_object(self, Bucket: str, Key: str):  # noqa: N803
        self.store.pop(Key, None)
        return {}


def _s3_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    s3 = S3Adapter(_FakeS3(), "pherix-test-bucket")

    @tool("s3", name="writeObject")
    def write_object(client, key: str):  # noqa: ANN001
        client.put_object(Bucket="pherix-test-bucket", Key=key, Body=b"hello")
        return {"ok": True}

    with agent_txn({"s3": s3}) as ctx:
        write_object(key="doc.bin")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakeRedis:
    """In-memory Redis: dump returns None for an absent key (snapshot path)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def dump(self, key: str):
        return None if key not in self.store else self.store[key].encode()

    def pttl(self, key: str) -> int:
        return -1

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _redis_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    redis = RedisAdapter(_FakeRedis())

    @tool("redis", name="setKey")
    def set_key(client, key: str):  # noqa: ANN001
        client.set(key, "hello")
        return {"ok": True}

    with agent_txn({"redis": redis}) as ctx:
        set_key(key="k")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakeMongoCollection:
    def __init__(self) -> None:
        self.docs: dict[Any, dict] = {}

    def find_one(self, filt: dict):
        return self.docs.get(filt["_id"])

    def replace_one(self, filt: dict, replacement: dict, upsert: bool = False):
        self.docs[filt["_id"]] = replacement
        return {"matchedCount": 1}

    def delete_one(self, filt: dict):
        self.docs.pop(filt["_id"], None)
        return {"deletedCount": 1}

    def insert_one(self, doc: dict) -> None:
        self.docs[doc["_id"]] = doc


class _FakeMongoDb:
    def __init__(self) -> None:
        self._colls: dict[str, _FakeMongoCollection] = {}

    def __getitem__(self, name: str) -> _FakeMongoCollection:
        return self._colls.setdefault(name, _FakeMongoCollection())


def _mongodb_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    mongo = MongoAdapter(_FakeMongoDb())

    @tool("mongodb", name="insertDoc")
    def insert_doc(db, collection: str, doc_id: str):  # noqa: ANN001
        db[collection].insert_one({"_id": doc_id, "name": "bob"})
        return {"ok": True}

    with agent_txn({"mongodb": mongo}) as ctx:
        insert_doc(collection="users", doc_id="u1")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakeDynamo:
    """Low-level in-memory DynamoDB: get_item returns {} for an absent key."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def get_item(self, TableName: str, Key: dict):  # noqa: N803
        pk = Key["pk"]["S"]
        return {} if pk not in self.items else {"Item": self.items[pk]}

    def put_item(self, TableName: str, Item: dict):  # noqa: N803
        self.items[Item["pk"]["S"]] = Item
        return {}

    def delete_item(self, TableName: str, Key: dict):  # noqa: N803
        self.items.pop(Key["pk"]["S"], None)
        return {}


def _dynamodb_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    ddb = DynamoDBAdapter(_FakeDynamo(), "pherix-test-table")

    @tool("dynamodb", name="putItem")
    def put_item(client, key: str):  # noqa: ANN001
        client.put_item(TableName="pherix-test-table", Item={"pk": {"S": key}, "v": {"S": "hello"}})
        return {"ok": True}

    with agent_txn({"dynamodb": ddb}) as ctx:
        put_item(key="doc")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakeGcsBlob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self._name = name

    def exists(self) -> bool:
        return self._name in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._name]

    def upload_from_string(self, data: bytes) -> None:
        self._store[self._name] = data

    def delete(self) -> None:
        self._store.pop(self._name, None)


class _FakeGcsClient:
    def __init__(self) -> None:
        self._buckets: dict[str, dict[str, bytes]] = {}

    def bucket(self, name: str):
        store = self._buckets.setdefault(name, {})

        class _Bucket:
            def blob(self, blob_name: str) -> _FakeGcsBlob:
                return _FakeGcsBlob(store, blob_name)

        return _Bucket()


def _gcs_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    gcs = GCSAdapter(_FakeGcsClient(), "pherix-test-bucket")

    @tool("gcs", name="saveBlob")
    def save_blob(client, key: str):  # noqa: ANN001
        client.bucket("pherix-test-bucket").blob(key).upload_from_string(b"hello")
        return {"ok": True}

    with agent_txn({"gcs": gcs}) as ctx:
        save_blob(key="doc.bin")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakeEs:
    """In-memory Elasticsearch: exists() is False for an absent document."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def exists(self, index: str, id: str) -> bool:  # noqa: A002 — ES kwarg name
        return id in self.docs

    def get(self, index: str, id: str):  # noqa: A002
        return {"_source": self.docs[id]}

    def index(self, index: str, id: str, document: dict, refresh: bool = False):  # noqa: A002
        self.docs[id] = document
        return {"result": "created"}

    def delete(self, index: str, id: str, refresh: bool = False):  # noqa: A002
        self.docs.pop(id, None)
        return {"result": "deleted"}


def _elasticsearch_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    es = ElasticsearchAdapter(_FakeEs(), "pherix-test-index")

    @tool("elasticsearch", name="indexDoc")
    def index_doc(client, key: str):  # noqa: ANN001
        client.index(index="pherix-test-index", id=key, document={"v": "hello"})
        return {"ok": True}

    with agent_txn({"elasticsearch": es}) as ctx:
        index_doc(key="doc")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakeMySqlCursor:
    """A sqlite-backed cursor speaking enough of pymysql's cursor surface.

    Mirrors the TS test's better-sqlite3 fake: SQLite speaks the same SAVEPOINT
    / ROLLBACK TO SAVEPOINT grammar, so the real MySQLAdapter code path runs
    offline. The two MySQL-specific grammars the adapter emits (the InnoDB DDL
    and ``ON DUPLICATE KEY UPDATE``) are rewritten to SQLite equivalents, and
    pymysql's ``%s`` placeholders to sqlite3's ``?`` — leaving the adapter
    untouched.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cur: sqlite3.Cursor | None = None

    @staticmethod
    def _rewrite(sql: str) -> str:
        s = sql.replace("%s", "?")
        s = s.replace(") ENGINE=InnoDB", ")").replace(")\n) ENGINE=InnoDB", ")\n)")
        s = s.replace("ENGINE=InnoDB", "")
        s = s.replace(
            "ON DUPLICATE KEY UPDATE version = version + 1",
            "ON CONFLICT(resource, key_json) DO UPDATE SET version = version + 1",
        )
        return s

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        self._cur = self._conn.execute(self._rewrite(sql), params)
        return self._cur

    def fetchone(self):
        return self._cur.fetchone() if self._cur is not None else None

    def __enter__(self) -> "_FakeMySqlCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeMySqlConn:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", isolation_level=None)
        self._conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")

    def cursor(self) -> _FakeMySqlCursor:
        return _FakeMySqlCursor(self._conn)


def _mysql_commit() -> tuple[list[Any], TxnState, str]:
    REGISTRY.clear()
    mysql = MySQLAdapter(_FakeMySqlConn())

    @tool("mysql", name="insertUser")
    def insert_user(conn, name: str):  # noqa: ANN001
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (name) VALUES (%s)", (name,))
        return name

    with agent_txn({"mysql": mysql}) as ctx:
        insert_user(name="bob")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


class _FakePgCursor:
    """A sqlite-backed cursor speaking enough of psycopg's cursor surface.

    Mirrors the TS test's better-sqlite3 ``FakePgClient``: SQLite speaks the same
    BEGIN / SAVEPOINT / ROLLBACK TO SAVEPOINT / COMMIT grammar the
    :class:`PostgresAdapter` drives, so the real adapter code path runs offline
    with no live Postgres. psycopg's ``%s`` placeholders are rewritten to
    sqlite3's ``?``; the version side-table's ``BIGINT`` column type is a valid
    SQLite type name (INTEGER affinity). The cursor is a context manager because
    the adapter drives it via ``with conn.cursor() as cur``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cur: sqlite3.Cursor | None = None

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        self._cur = self._conn.execute(sql.replace("%s", "?"), params or ())
        return self._cur

    def fetchone(self):
        return self._cur.fetchone() if self._cur is not None else None

    def __enter__(self) -> "_FakePgCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakePgConn:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", isolation_level=None)
        self._conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")

    def cursor(self) -> _FakePgCursor:
        return _FakePgCursor(self._conn)


def _postgres_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: a Postgres insert that commits (STAGED -> APPLIED).

    Guarded like ``_git_commit``: the adapter's ``__init__`` does ``import
    psycopg`` as an install-check, so we skip cleanly when psycopg is absent
    (the same machine runs both halves, so a single Python-side skip keeps the
    offline suite green). The TS half is backed by better-sqlite3 — always
    present as a devDependency — so the skip is one-sided and symmetric with the
    git scenario."""
    pytest.importorskip("psycopg")
    REGISTRY.clear()
    pg = PostgresAdapter(_FakePgConn())

    @tool("postgres", name="insertUser")
    def insert_user(conn, name: str):  # noqa: ANN001
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (name) VALUES (%s)", (name,))
        return name

    with agent_txn({"postgres": pg}) as ctx:
        insert_user(name="bob")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


def _filesystem_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: a filesystem write that commits (STAGED -> APPLIED).

    Unlike the SQL commit scenarios, the FsHandle records a write key
    automatically — so this also asserts cross-SDK parity of the
    content-addressed filesystem version: writing the SAME raw bytes (``b"hello"``
    / ``enc("hello")``) makes the write-key version the sha256 of those identical
    bytes on both sides, byte-for-byte. A real temp dir is used and removed in a
    ``finally`` (the TS half does the same)."""
    REGISTRY.clear()
    root = Path(tempfile.mkdtemp(prefix="pherix_fs_parity_"))
    fs = FilesystemAdapter(root)

    @tool("fs", name="writeFile")
    def write_file(handle, path: str):  # noqa: ANN001
        handle.write(path, b"hello")
        return {"ok": True}

    try:
        with agent_txn({"fs": fs}) as ctx:
            write_file(path="note.txt")
        return list(ctx.txn.effects), ctx.txn.state, "ok"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _memory_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: a governed-memory ``remember`` that commits (STAGED ->
    APPLIED). Unlike the other per-adapter commit scenarios, the memory handle
    records a write key automatically — so this also asserts cross-SDK parity of
    the content-addressed version: ``remember`` of a plain STRING value makes the
    write-key version the sha256 of the SAME bytes on both sides (a non-string
    value would route through ``json.dumps`` vs ``JSON.stringify`` and their
    key-spacing differs, so the value is deliberately a string)."""
    REGISTRY.clear()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    mem = MemoryAdapter(conn)

    @tool("memory", name="remember")
    def remember(handle, key: str, value: str):  # noqa: ANN001
        handle.remember(key, value)
        return {"ok": True}

    with agent_txn({"memory": mem}) as ctx:
        remember(key="fact", value="hello")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


def _git_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: a git op against a real temp repo that commits.

    Guarded: skips cleanly when the ``git`` binary is absent (same machine runs
    both halves, so a single skip on the Python side keeps the offline suite
    green on either side)."""
    if shutil.which("git") is None:
        pytest.skip("git binary not installed")
    REGISTRY.clear()

    def _g(root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)

    repo = Path(tempfile.mkdtemp(prefix="pherix_git_parity_"))
    _g(repo, "init", "-q")
    _g(repo, "config", "user.email", "t@example.com")
    _g(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("v1\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "c1")
    git_adapter = GitAdapter(repo)

    @tool("git", name="runGit")
    def run_git(handle, command: str):  # noqa: ANN001
        return handle.run(command)

    try:
        with agent_txn({"git": git_adapter}) as ctx:
            run_git(command="status")
        return list(ctx.txn.effects), ctx.txn.state, "ok"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def publish(self, topic: str, message: Any) -> Any:
        self.published.append((topic, message))
        return {"acked": True}


def _messagequeue_gate() -> tuple[list[Any], TxnState, str]:
    """Irreversible + gate: a publish with no compensator. commit() gates ->
    ROLLED_BACK, effect GATED. The publish never fires."""
    REGISTRY.clear()
    broker = _FakeBroker()
    emit = publish_tool("emit_order", broker=broker)

    outcome = "ok"
    captured: list[Any] = []
    try:
        with agent_txn({"mq": MQAdapter()}) as ctx:
            captured.append(ctx)
            emit(topic="orders", message={"id": 1})
    except GateBlocked:
        outcome = "GateBlocked"
    ctx = captured[0]
    assert not broker.published, "gate must stop the publish from firing"
    return list(ctx.txn.effects), ctx.txn.state, outcome


def _rest_gate() -> tuple[list[Any], TxnState, str]:
    """Irreversible + gate: a REST POST with no compensator. commit() gates ->
    ROLLED_BACK, effect GATED. The call never fires."""
    REGISTRY.clear()
    calls: list[Any] = []

    def transport(method: str, url: str, **kw: Any) -> Any:
        calls.append((method, url, kw))
        return {"status": 201}

    create = rest_tool(
        "create_user", method="POST", url="https://api/users", transport=transport
    )

    outcome = "ok"
    captured: list[Any] = []
    try:
        with agent_txn({"rest": RESTAdapter()}) as ctx:
            captured.append(ctx)
            create(json={"name": "ada"})
    except GateBlocked:
        outcome = "GateBlocked"
    ctx = captured[0]
    assert not calls, "gate must stop the REST call from firing"
    return list(ctx.txn.effects), ctx.txn.state, outcome


@dataclass(frozen=True)
class Scenario:
    name: str  # MUST match the branch name in runner.mts
    run: Callable[[], tuple[list[Any], TxnState, str]]


SCENARIOS = [
    Scenario("reversible_commit", _reversible_commit),
    Scenario("irreversible_gate", _irreversible_gate),
    Scenario("isolation_conflict", _isolation_conflict),
    # EXTENSION POINT: one Scenario per newly-ported TS adapter, each paired
    # with a same-named branch in pherix-ts/test/parity/runner.mts. See the
    # module docstring's "Extending per adapter" section.
    # Reversible adapters — one commit scenario each (STAGED -> APPLIED).
    Scenario("s3_commit", _s3_commit),
    Scenario("redis_commit", _redis_commit),
    Scenario("mongodb_commit", _mongodb_commit),
    Scenario("dynamodb_commit", _dynamodb_commit),
    Scenario("gcs_commit", _gcs_commit),
    Scenario("elasticsearch_commit", _elasticsearch_commit),
    Scenario("mysql_commit", _mysql_commit),
    Scenario("postgres_commit", _postgres_commit),
    Scenario("filesystem_commit", _filesystem_commit),
    Scenario("memory_commit", _memory_commit),
    Scenario("git_commit", _git_commit),
    # Irreversible adapters — one gate scenario each (GATED -> ROLLED_BACK).
    Scenario("rest_gate", _rest_gate),
    Scenario("messagequeue_gate", _messagequeue_gate),
]


# Maps each parity scenario to the adapter resource it exercises. The coverage
# gate below uses this to assert that EVERY adapter shipped under
# ``pherix/core/adapters`` has at least one parity scenario — so a newly-added
# adapter that lacks a Py<->TS scenario fails the suite loudly instead of
# silently drifting out of the cross-SDK contract. Keep it in lockstep with
# :data:`SCENARIOS` (the gate asserts that too).
SCENARIO_RESOURCES: dict[str, str] = {
    "reversible_commit": "sql",
    "irreversible_gate": "http",
    "isolation_conflict": "sql",
    "s3_commit": "s3",
    "redis_commit": "redis",
    "mongodb_commit": "mongodb",
    "dynamodb_commit": "dynamodb",
    "gcs_commit": "gcs",
    "elasticsearch_commit": "elasticsearch",
    "mysql_commit": "mysql",
    "postgres_commit": "postgres",
    "filesystem_commit": "fs",
    "memory_commit": "memory",
    "git_commit": "git",
    "rest_gate": "rest",
    "messagequeue_gate": "mq",
}


def _shipped_adapter_resources() -> set[str]:
    """Every resource name shipped by a concrete adapter under
    ``pherix/core/adapters``.

    A concrete adapter is a class DEFINED in an adapter submodule (not merely
    imported into it) that carries a non-empty class-level ``name`` string AND a
    per-effect ``apply`` method — the protocol entry point. This deliberately
    excludes the abstract base (``name`` is ``""``), ``SnapshotHandle``, and
    handle helpers like ``FsHandle`` (no ``name``). Driver imports are lazy in
    every adapter's ``__init__``, so importing the modules here needs zero
    third-party packages.
    """
    import pherix.core.adapters as pkg

    names: set[str] = set()
    for modinfo in pkgutil.iter_modules(pkg.__path__):
        if modinfo.name == "base":
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{modinfo.name}")
        for obj in vars(mod).values():
            if not isinstance(obj, type) or obj.__module__ != mod.__name__:
                continue  # skip non-classes and classes imported from elsewhere
            name = getattr(obj, "name", "")
            if isinstance(name, str) and name and callable(getattr(obj, "apply", None)):
                names.add(name)
    return names


def _ts_adapter_resources() -> set[str]:
    """Every resource name shipped by a concrete adapter under
    ``pherix-ts/src/adapters`` — the TypeScript twin of
    :func:`_shipped_adapter_resources`.

    A concrete TS adapter declares its resource as a class-field literal
    ``readonly name = "<resource>"`` (e.g. ``sql.ts``: ``readonly name =
    "sql"``). The abstract base declares ``readonly name: string`` with NO
    literal, so it is excluded by construction; ``index.ts`` only re-exports and
    carries no declaration. The source is read as TEXT — there is no Node
    dependency — so this enumerates the TS inventory on a Node-less machine
    exactly as the Python side does, which is the whole point: the cross-SDK
    inventory contract must hold where the journal-diff test is skipped.
    """
    names: set[str] = set()
    for ts_file in sorted(TS_ADAPTERS_DIR.glob("*.ts")):
        for match in re.finditer(r'readonly\s+name\s*=\s*"([^"]+)"', ts_file.read_text()):
            names.add(match.group(1))
    return names


def _ts_runner_scenarios() -> set[str]:
    """The scenario names the TS parity runner dispatches — the keys of the
    ``SCENARIOS`` object literal in ``pherix-ts/test/parity/runner.mts``.

    Parsed statically (no Node): the ``SCENARIOS`` object literal is isolated by
    brace-matching from its declaration to the matching close brace, then each
    ``key:`` at the head of a non-comment line is taken as a scenario name. The
    matching Python set is ``{s.name for s in SCENARIOS}``; the symmetry gate
    asserts the two are identical, so a scenario added on one SDK without its
    twin on the other fails loudly even on a Node-less machine — where the
    journal-diff test that would otherwise catch it is skipped.
    """
    src = RUNNER.read_text()
    brace = src.index("{", src.index("const SCENARIOS"))
    depth = 0
    end = brace
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    names: set[str] = set()
    for line in src[brace + 1 : end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        match = re.match(r"([A-Za-z_]\w*)\s*:", stripped)
        if match:
            names.add(match.group(1))
    return names


# -- the parity assertion ----------------------------------------------------


def _run_python(scenario: Scenario) -> dict:
    effects, final_state, outcome = scenario.run()
    return _json_normalized(_canonical_journal(effects, final_state, outcome))


def _run_node(scenario: Scenario) -> dict:
    assert RUNNER.exists(), f"missing parity runner: {RUNNER}"
    runner = NPX or NODE
    cmd = (
        [NPX, "tsx", str(RUNNER), scenario.name]
        if NPX
        else [NODE, str(RUNNER), scenario.name]
    )
    proc = subprocess.run(
        cmd,
        cwd=str(PHERIX_TS),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"node runner failed for scenario {scenario.name!r} "
        f"(via {runner}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    # Round-trip the Node JSON too so both sides are compared in the same
    # normalized form (a no-op for valid JSON, but keeps the symmetry explicit).
    return _json_normalized(json.loads(proc.stdout))


@pytest.mark.skipif(NODE is None, reason="Node not installed")
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_sdk_journal_parity(scenario: Scenario):
    """The Python and TS SDKs produce structurally identical journals."""
    assert NPX is not None or NODE is not None, "no node/npx on PATH"
    py = _run_python(scenario)
    ts = _run_node(scenario)

    # Compare the high-level facts first so a failure points at the axis that
    # diverged (final state / outcome / effect count) before the per-effect diff.
    assert ts["final_state"] == py["final_state"], (
        f"{scenario.name}: final transaction state differs "
        f"(py={py['final_state']!r}, ts={ts['final_state']!r})"
    )
    assert ts["outcome"] == py["outcome"], (
        f"{scenario.name}: commit outcome differs "
        f"(py={py['outcome']!r}, ts={ts['outcome']!r})"
    )
    assert len(ts["effects"]) == len(py["effects"]), (
        f"{scenario.name}: journal length differs "
        f"(py={len(py['effects'])}, ts={len(ts['effects'])})"
    )
    # Then the full per-effect structural diff (order, status, keys, lane).
    assert ts["effects"] == py["effects"], (
        f"{scenario.name}: journals diverge.\nPY:  {py['effects']}\nTS:  {ts['effects']}"
    )


# -- the coverage gate (pure Python — runs without Node) ---------------------


def test_parity_scenarios_cover_every_adapter():
    """GATE: every adapter shipped under ``pherix/core/adapters`` has a Py<->TS
    parity scenario, and the scenario list ⇔ resource map stay in lockstep.

    This is what makes the parity suite a CONTRACT rather than a snapshot: ship a
    new adapter on one side without a paired scenario and this fails loudly,
    naming the uncovered resource — instead of the gap going unnoticed until the
    two SDKs have quietly diverged. It needs no Node (it never diffs a journal),
    so the contract holds even on a Node-less machine.
    """
    # 1. The scenario list and the resource map must describe the same set of
    #    scenarios — a scenario with no mapped resource (or vice versa) means the
    #    gate below is reasoning over a stale map.
    scenario_names = {s.name for s in SCENARIOS}
    mapped_names = set(SCENARIO_RESOURCES)
    assert mapped_names == scenario_names, (
        "SCENARIO_RESOURCES drifted from SCENARIOS: "
        f"only in map={sorted(mapped_names - scenario_names)}, "
        f"only in SCENARIOS={sorted(scenario_names - mapped_names)}"
    )

    # 2. Bidirectional coverage: every shipped adapter resource is exercised by a
    #    scenario, and no scenario claims a resource that no adapter ships.
    shipped = _shipped_adapter_resources()
    covered = set(SCENARIO_RESOURCES.values())
    missing = shipped - covered
    phantom = covered - shipped
    assert not missing, (
        f"adapters with no Py<->TS parity scenario: {sorted(missing)}. "
        "Add a Scenario to SCENARIOS (Python) + a same-named branch to "
        "pherix-ts/test/parity/runner.mts (TS), and register it in "
        "SCENARIO_RESOURCES."
    )
    assert not phantom, (
        f"SCENARIO_RESOURCES references resources no adapter ships: {sorted(phantom)}. "
        "Fix the mapping or remove the stale scenario."
    )


def test_scenario_resource_map_matches_reality():
    """The declared SCENARIO_RESOURCES entry for each runnable Python scenario
    matches the resource its effects actually carry — so the map (which the gate
    above trusts) cannot quietly lie about WHICH adapter a scenario covers.

    Scenarios that ``pytest.skip`` for a missing optional dependency (git binary,
    psycopg) are tolerated: their declared resource is taken on faith, exactly as
    the journal-diff test would skip them. Every other scenario is run in-process
    (no Node) and its emitted resources are checked against the declaration.
    """
    for scenario in SCENARIOS:
        try:
            effects, _state, _outcome = scenario.run()
        except pytest.skip.Exception:
            continue  # optional dependency absent — declaration taken on faith
        declared = SCENARIO_RESOURCES[scenario.name]
        emitted = {e.resource for e in effects}
        assert emitted == {declared}, (
            f"{scenario.name}: declared resource {declared!r} but effects carry "
            f"{sorted(emitted)} — fix SCENARIO_RESOURCES."
        )


# -- the symmetric cross-SDK gates (pure Python — run without Node) ----------


def test_adapter_inventory_symmetric_across_sdks():
    """GATE: the Python and TypeScript SDKs ship the EXACT same set of adapter
    resources — neither side has an adapter the other lacks.

    :func:`test_parity_scenarios_cover_every_adapter` proves every *Python*
    adapter has a parity scenario, but it never reads the TS source — so an
    adapter ported to one SDK and not the other slips through: the missing
    twin is invisible to a gate that only walks one side's inventory, and the
    ``one system`` claim quietly breaks. This gate diffs the two inventories
    directly. Both sides are static text parses, so it holds on a Node-less
    machine — the same place the journal-diff test (``skipif NODE is None``)
    cannot help.
    """
    if not TS_ADAPTERS_DIR.is_dir():
        pytest.skip(f"pherix-ts source tree absent: {TS_ADAPTERS_DIR}")
    py = _shipped_adapter_resources()
    ts = _ts_adapter_resources()
    py_only = py - ts
    ts_only = ts - py
    assert not py_only, (
        f"adapters shipped in Python but missing from pherix-ts: {sorted(py_only)}. "
        "Port the adapter to pherix-ts/src/adapters so the two SDKs stay one system."
    )
    assert not ts_only, (
        f"adapters shipped in pherix-ts but missing from Python: {sorted(ts_only)}. "
        "Add the adapter under pherix/core/adapters so the two SDKs stay one system."
    )


def test_parity_scenarios_symmetric_across_sdks():
    """GATE: the Python ``SCENARIOS`` list and the TS runner's ``SCENARIOS`` map
    name the SAME set of scenarios.

    The journal-diff test pairs a Python scenario with a same-named TS branch —
    but it is ``skipif(NODE is None)``, so on a Node-less machine a Python
    scenario whose TS branch was never added (or vice versa) passes silently
    until someone runs it under Node. This static gate makes the name-pairing a
    contract that holds WITHOUT Node, naming the unpaired scenario in either
    direction.
    """
    if not RUNNER.exists():
        pytest.skip(f"pherix-ts parity runner absent: {RUNNER}")
    py = {s.name for s in SCENARIOS}
    ts = _ts_runner_scenarios()
    py_only = py - ts
    ts_only = ts - py
    assert not py_only, (
        f"parity scenarios defined in Python with no TS branch: {sorted(py_only)}. "
        "Add a same-named entry to SCENARIOS in pherix-ts/test/parity/runner.mts."
    )
    assert not ts_only, (
        f"parity scenarios defined in the TS runner with no Python twin: {sorted(ts_only)}. "
        "Add a same-named Scenario to SCENARIOS in tests/test_sdk_parity.py."
    )
