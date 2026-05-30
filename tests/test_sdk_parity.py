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
Mirrors ``test_governance_js_conformance.py``: the whole module is
``skipif(node is None)`` so the offline Python suite stays green on a machine
without Node. The Node runner is run from source via ``npx tsx`` (already a
``pherix-ts`` devDependency) — no build step. If ``node_modules`` is absent the
subprocess fails and the test reports it loudly rather than silently passing.

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

import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.memory import MemoryAdapter
from pherix.core.adapters.dynamodb import DynamoDBAdapter
from pherix.core.adapters.elasticsearch import ElasticsearchAdapter
from pherix.core.adapters.gcs import GCSAdapter
from pherix.core.adapters.git import GitAdapter
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.messagequeue import MQAdapter, publish_tool
from pherix.core.adapters.mongodb import MongoAdapter
from pherix.core.adapters.mysql import MySQLAdapter
from pherix.core.adapters.redis import RedisAdapter
from pherix.core.adapters.rest import RESTAdapter, rest_tool
from pherix.core.adapters.s3 import S3Adapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.dry_run import dry_run
from pherix.core.isolation import IsolationConflict
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import REGISTRY, tool
from pherix.core.transaction import TxnState

NODE = shutil.which("node")
NPX = shutil.which("npx")
PHERIX_TS = Path(__file__).resolve().parent.parent / "pherix-ts"
RUNNER = PHERIX_TS / "test" / "parity" / "runner.mts"

pytestmark = pytest.mark.skipif(NODE is None, reason="Node not installed")


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


def _fs_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: write one file in a temp rooted directory, commits normally.

    Exercises FilesystemAdapter's copy-on-write snapshot path. The write_key
    records the sha256 of the file content after the write, so parity also
    verifies the content-hash versioning scheme matches across languages."""
    REGISTRY.clear()
    root = Path(tempfile.mkdtemp(prefix="pherix_fs_parity_"))
    fs = FilesystemAdapter(root)

    @tool("fs", name="writeFile")
    def write_file(handle, content: str):  # noqa: ANN001
        handle.write("data.txt", content.encode())
        return {"ok": True}

    try:
        with agent_txn({"fs": fs}) as ctx:
            write_file(content="hello")
        return list(ctx.txn.effects), ctx.txn.state, "ok"
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


def _memory_commit() -> tuple[list[Any], TxnState, str]:
    """Reversible: a memory remember that commits. write_key carries a sha256
    version (content-addressed), so parity verifies both lane and versioning
    match across languages."""
    REGISTRY.clear()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    adapter = MemoryAdapter(conn)

    @tool("memory", name="rememberFact")
    def remember_fact(handle, key: str, value: str):  # noqa: ANN001
        handle.remember(key, value)
        return {"ok": True}

    with agent_txn({"memory": adapter}) as ctx:
        remember_fact(key="greeting", value="hello")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


def _dry_run_commit() -> tuple[list[Any], TxnState, str]:
    """Dry-run: a single reversible SQL write runs, then is rolled back.

    The effect lands in the journal with status COMPENSATED (ran and then
    unwound via snapshot restore) and the transaction ends ROLLED_BACK — the
    same ROLLED_BACK value that explicit rollback and gate-block produce, but
    reached via the dry-run finalise path instead of an error path.  The world
    (the in-memory SQLite DB) is byte-identical to its state before the dry-run.
    Parity asserts that both languages produce the same journal: one COMPENSATED
    reversible effect, final_state=rolled_back, outcome=ok."""
    REGISTRY.clear()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE notes (body TEXT)")
    sql = SQLiteAdapter(conn)

    @tool("sql", name="insertNote")
    def insert_note(conn, body: str):  # noqa: ANN001
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        return {"ok": True}

    with dry_run({"sql": sql}) as ctx:
        insert_note(body="hello")
    return list(ctx.txn.effects), ctx.txn.state, "ok"


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
    Scenario("git_commit", _git_commit),
    Scenario("fs_commit", _fs_commit),
    # Irreversible adapters — one gate scenario each (GATED -> ROLLED_BACK).
    Scenario("rest_gate", _rest_gate),
    Scenario("messagequeue_gate", _messagequeue_gate),
    # Memory adapter — reversible commit with write_key (content-addressed sha256).
    Scenario("memory_commit", _memory_commit),
    # Dry-run path — reversible write runs and is rolled back; COMPENSATED + ROLLED_BACK.
    Scenario("dry_run_commit", _dry_run_commit),
]


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
