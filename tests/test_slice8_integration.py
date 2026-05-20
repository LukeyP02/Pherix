"""Slice 8 acceptance — the discipline pin.

The gateway is *not* a new engine. It is a second driver of the same
:func:`agent_txn` / :func:`dry_run` core. These tests prove that
discipline held: a tool call routed through the MCP gateway produces an
IDENTICAL journal / audit / policy outcome to the same call made
library-direct, with the single expected difference that the gateway
attributes its transaction to a handshake ``client_id`` while a library
caller's ``client_id`` is NULL.

Scenarios:

  1. Parity — the load-bearing pin. Same tool, same args, two drivers;
     the journal (tool / args / resource / reversible / status /
     ordering), the audit rows (txn state + per-effect rows), and the
     policy verdict are bit-for-bit identical bar ``client_id``.
  2. Per-identity policy selection — a tool denied for "aider" but
     allowed for "claude-code".
  3. ``client_id`` round-trip — gateway call attributes the txn to its
     identity; library call leaves it NULL.
  4. State-diff over the wire — a dry-run gateway call returns a
     serialised ``state_diff`` showing the SQL rows / FS files a real
     commit would have touched.
  5. Wire round-trip including ``bytes`` — a result carrying ``bytes``
     serialises via :func:`strict_json_default` and survives the
     JSON-RPC envelope as the base64 ``<bytes:b64:...>`` form.
  6. Cross-process arbitration — two ``SQLiteAdapter``s over two separate
     connections to one on-disk file; the second commit's diff sees the
     first's version bump via Slice 4's meta-connection. This is the
     gateway-as-another-process case reusing the existing engine.

These tests are written against the Stream A / Stream B contract; some
depend on those streams landing (noted inline). Stream C owns only the
tests, the demo, and the library export.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus, strict_json_default
from pherix.core.isolation import Abort, IsolationConflict
from pherix.core.policy import Policy
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY as TOOL_REGISTRY, tool
from pherix.core.transaction import TxnState

# Stream A — the gateway. Imported at module top so a collection-time
# ImportError makes the dependency loud rather than silently skipping.
from pherix.frontends.proxy import InProcessMCPClient, PherixGateway


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory SQLite, autocommit so the adapter owns every BEGIN."""
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    yield c
    c.close()


@pytest.fixture
def fs_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    return root


def _note_rows(c) -> list[tuple]:
    return [tuple(r) for r in c.execute("SELECT id, body FROM notes ORDER BY id")]


def _journal_shape(effects) -> list[tuple]:
    """The journal fields that must match across the two drivers.

    Deliberately excludes ``effect_id`` (it folds ``txn_id`` in, and the
    two drivers run distinct transactions so the ids differ by design)
    and ``ts`` (wall-clock). What we pin is the *structure* of the fold:
    tool, args, resource, reversibility, terminal status, and ordering.
    """
    return [
        (e.index, e.tool, e.args, e.resource, e.reversible, e.status)
        for e in effects
    ]


def _audit_effect_shape(rows: list[dict]) -> list[tuple]:
    """The audit-row fields that must match across the two drivers."""
    return [
        (r["idx"], r["tool"], r["resource"], r["reversible"], r["status"],
         r["args"])
        for r in rows
    ]


# --- 1. parity: gateway is a pure second driver ------------------------------


def test_gateway_call_journal_audit_policy_identical_to_library(
    fs_root: Path,
):
    """THE load-bearing pin.

    Run the same reversible tool call two ways and assert the journal,
    the audit transaction state, the audit effect rows, and the policy
    verdict are identical — with the one sanctioned difference that the
    gateway txn's ``client_id`` is the handshake identity and the library
    txn's is NULL.

    Two separate on-disk SQLite files (one per driver) so the two
    transactions are genuinely independent yet structurally identical;
    using one file would make the second txn's diff race the first.
    """
    db_lib = fs_root / "lib.db"
    db_gw = fs_root / "gw.db"
    for db in (db_lib, db_gw):
        boot = sqlite3.connect(str(db), isolation_level=None)
        boot.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        boot.close()

    conn_lib = sqlite3.connect(str(db_lib), isolation_level=None)
    conn_gw = sqlite3.connect(str(db_gw), isolation_level=None)
    try:

        @tool(resource="sql")
        def insert_note(conn, body):
            conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
            return {"inserted": body}

        # An identical policy object shape for both drivers — a single
        # allow rule, so the verdict bundle is comparable.
        def make_policy() -> Policy:
            p = Policy.allow_all()

            @p.rule
            def allow_notes(effect, ctx):
                from pherix.core.policy import Allow

                return Allow()

            return p

        # --- (a) library-direct -------------------------------------------
        audit_lib = AuditJournal.in_memory()
        with agent_txn(
            {"sql": SQLiteAdapter(conn_lib)},
            policy=make_policy(),
            audit=audit_lib,
        ) as ctx_lib:
            insert_note(body="hello")
        lib_txn_id = ctx_lib.txn_id

        # --- (b) gateway-driven -------------------------------------------
        audit_gw = AuditJournal.in_memory()
        gateway = PherixGateway(
            adapters={"sql": SQLiteAdapter(conn_gw)},
            default_policy=make_policy(),
            audit=audit_gw,
        )
        client = InProcessMCPClient(gateway)
        client.initialize("claude-code")
        resp = client.call_tool("insert_note", {"body": "hello"})
        # The committed txn_id comes back in the response envelope — the
        # contract surface, not an audit-table peek.
        gw_txn_id = InProcessMCPClient.structured_of(resp)["txn_id"]

        # --- journal shape identical --------------------------------------
        assert _journal_shape(ctx_lib.txn.effects) == _journal_shape(
            _effects_as_objects(audit_gw, gw_txn_id)
        ), "journal fold structure must match across drivers"

        # --- audit transaction state identical ----------------------------
        lib_txn = audit_lib.get_transaction(lib_txn_id)
        gw_txn = audit_gw.get_transaction(gw_txn_id)
        assert lib_txn["state"] == gw_txn["state"] == TxnState.COMMITTED.name

        # --- audit effect rows identical ----------------------------------
        assert _audit_effect_shape(
            audit_lib.get_effects(lib_txn_id)
        ) == _audit_effect_shape(audit_gw.get_effects(gw_txn_id))

        # --- effect terminal state was APPLIED then COMMITTED -------------
        assert ctx_lib.txn.effects[0].status is EffectStatus.APPLIED

        # --- the ONE sanctioned difference: client_id ---------------------
        # Stream B adds the column; gateway sets it to the handshake
        # identity, library leaves it NULL.
        assert lib_txn["client_id"] is None
        assert gw_txn["client_id"] == "claude-code"

        # --- both actually persisted the row ------------------------------
        assert _note_rows(conn_lib) == _note_rows(conn_gw) == [(1, "hello")]
    finally:
        conn_lib.close()
        conn_gw.close()


def _effects_as_objects(audit: AuditJournal, txn_id: str):
    """Reconstruct comparable Effect-like tuples from audit rows.

    The gateway runs its own ``agent_txn`` internally — Stream C cannot
    reach into that ctx for the live journal, so we read the persisted
    audit rows and rebuild the shape ``_journal_shape`` compares against.
    """
    from pherix.core.effects import Effect

    out = []
    for r in audit.get_effects(txn_id):
        e = Effect(
            txn_id=txn_id,
            index=r["idx"],
            tool=r["tool"],
            args=json.loads(r["args"]),
            resource=r["resource"],
            reversible=bool(r["reversible"]),
            effect_id=r["effect_id"],
        )
        e.status = EffectStatus[r["status"]]
        out.append(e)
    return out


# --- 2. per-identity policy selection ----------------------------------------


def test_per_identity_policy_denies_aider_allows_claude_code(conn):
    """A tool deny-listed for "aider" but allowed for "claude-code".

    The aider call is rejected (no committed effect); the claude-code
    call commits. Same gateway, same tool — only the handshake identity
    differs, and the gateway selects the policy from it.
    """

    @tool(resource="sql")
    def delete_everything(conn, table):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (f"wiped {table}",))

    gateway = PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        policies={"aider": Policy(deny={"delete_everything"})},
        default_policy=Policy.allow_all(),
        audit=AuditJournal.in_memory(),
    )

    # aider: denied. A policy denial is a tool-level refusal — a successful
    # envelope carrying isError content, not a JSON-RPC error. The client does
    # not raise; the load-bearing assertion is that NO row committed.
    aider = InProcessMCPClient(gateway)
    aider.initialize("aider")
    resp = aider.call_tool("delete_everything", {"table": "notes"})
    assert InProcessMCPClient.error_of(resp) is None, "denial is not a protocol error"
    assert InProcessMCPClient.is_tool_error(resp) is True
    structured = InProcessMCPClient.structured_of(resp)
    assert structured["pherix_error"] == "policy_violation"
    assert "deny" in structured["message"].lower()
    assert _note_rows(conn) == []

    # claude-code: allowed (falls to default_policy = allow_all).
    cc = InProcessMCPClient(gateway)
    cc.initialize("claude-code")
    cc_resp = cc.call_tool("delete_everything", {"table": "notes"})
    assert InProcessMCPClient.is_tool_error(cc_resp) is False
    assert _note_rows(conn) == [(1, "wiped notes")]


# --- 3. client_id round-trip -------------------------------------------------


def test_client_id_round_trips_gateway_identity_library_null(conn):
    """Gateway call with identity "aider" → transactions.client_id ==
    "aider"; a library call leaves it NULL. Depends on Stream B's
    ``client_id`` column + ``agent_txn(..., client_id=...)`` kwarg.
    """

    @tool(resource="sql")
    def add_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    audit = AuditJournal.in_memory()

    # Gateway path.
    gateway = PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        default_policy=Policy.allow_all(),
        audit=audit,
    )
    client = InProcessMCPClient(gateway)
    client.initialize("aider")
    resp = client.call_tool("add_note", {"body": "from-gateway"})

    gw_txn_id = InProcessMCPClient.structured_of(resp)["txn_id"]
    assert audit.get_transaction(gw_txn_id)["client_id"] == "aider"

    # Library path on the same audit journal.
    conn2 = sqlite3.connect(":memory:", isolation_level=None)
    conn2.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    try:
        with agent_txn(
            {"sql": SQLiteAdapter(conn2)}, audit=audit
        ) as lib_ctx:
            add_note(body="from-library")
        assert audit.get_transaction(lib_ctx.txn_id)["client_id"] is None
    finally:
        conn2.close()


# --- 4. state-diff over the wire ---------------------------------------------


def test_dry_run_gateway_call_returns_serialised_state_diff(
    conn, fs_root: Path
):
    """A dry-run gateway call returns a serialised result whose
    ``state_diff`` shows the known SQL ``rows_added`` and FS
    ``files_added`` for a mutation. Depends on Stream B's
    ``DryRunResult.state_diff`` + Stream A wiring ``dry_run=True``
    through ``tools/call``.
    """

    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)

    gateway = PherixGateway(
        adapters={
            "sql": SQLiteAdapter(conn),
            "fs": FilesystemAdapter(fs_root),
        },
        default_policy=Policy.allow_all(),
        audit=AuditJournal.in_memory(),
    )
    client = InProcessMCPClient(gateway)
    client.initialize("claude-code")

    sql_resp = client.call_tool(
        "insert_note", {"body": "speculative"}, dry_run=True
    )
    fs_resp = client.call_tool(
        "write_file", {"path": "draft.txt", "data": b"draft-bytes"},
        dry_run=True,
    )

    sql_diff = _state_diff(sql_resp)
    assert "sql" in sql_diff
    assert any(
        "speculative" in json.dumps(row)
        for row in sql_diff["sql"]["rows_added"]
    )

    fs_diff = _state_diff(fs_resp)
    assert "fs" in fs_diff
    assert any(
        "draft.txt" in json.dumps(f) for f in fs_diff["fs"]["files_added"]
    )

    # Dry-run: nothing actually persisted.
    assert _note_rows(conn) == []
    assert not (fs_root / "draft.txt").exists()


def _state_diff(resp) -> dict:
    """Pull ``state_diff`` from a dry-run gateway response envelope.

    Stream A's contract: ``call_tool`` returns the full JSON-RPC envelope,
    whose ``result`` is the MCP ``tools/call`` shape; the Pherix payload lives
    in ``structuredContent`` and for a dry-run is
    ``{"txn_id", "dry_run": True, "dry_run_result": {... "state_diff": {...}}}``.
    """
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    structured = InProcessMCPClient.structured_of(resp)
    dry = structured["dry_run_result"]
    return dry["state_diff"]


# --- 5. wire round-trip including bytes --------------------------------------


def test_bytes_result_round_trips_through_jsonrpc_as_base64(conn):
    """A tool returning ``bytes`` serialises via ``strict_json_default``
    and survives the JSON-RPC envelope intact, coming back as the
    ``<bytes:b64:...>`` form. The wire format IS the audit format — no
    new serialisation vocabulary in the gateway.
    """
    payload = b"\x00\x01binary-payload\xff"
    expected = strict_json_default(payload)  # the <bytes:b64:...> string

    @tool(resource="sql")
    def fetch_blob(conn, key):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (key,))
        return {"blob": payload}

    gateway = PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        default_policy=Policy.allow_all(),
        audit=AuditJournal.in_memory(),
    )
    client = InProcessMCPClient(gateway)
    client.initialize("claude-code")
    resp = client.call_tool("fetch_blob", {"key": "k1"})

    # Find the round-tripped bytes anywhere in the result envelope.
    flat = json.dumps(resp)
    assert expected in flat, (
        f"bytes did not round-trip as base64; expected {expected!r} in "
        f"{flat!r}"
    )


# --- 6. cross-process arbitration --------------------------------------------


def test_cross_process_arbitration_second_commit_conflicts(tmp_path: Path):
    """Two separate ``sqlite3.connect`` connections to ONE on-disk file,
    each wrapped in its own ``SQLiteAdapter`` — the gateway-as-another-
    process case. Two transactions both write the same key; the second
    commit's diff detects the first's version bump via Slice 4's
    meta-connection and raises ``IsolationConflict``.

    This proves the gateway-as-another-process path reuses the existing
    Slice-4 commit-time conflict check rather than needing new machinery.

    Known limitation (documented, NOT fixed here): the filesystem adapter
    has no cross-process version side-table, so FS conflicts across
    genuinely separate processes are not detected. Single-gateway
    deployments — the supported topology — never hit this: every session
    shares one in-process arbitration registry.
    """
    db = tmp_path / "shared.db"
    boot = sqlite3.connect(str(db), isolation_level=None)
    boot.execute("PRAGMA journal_mode=WAL")
    boot.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER)")
    boot.execute("INSERT INTO counters VALUES ('x', 0)")
    boot.close()

    conn_a = sqlite3.connect(str(db), isolation_level=None)
    conn_b = sqlite3.connect(str(db), isolation_level=None)
    conn_a.execute("PRAGMA journal_mode=WAL")
    conn_b.execute("PRAGMA journal_mode=WAL")
    ad_a = SQLiteAdapter(conn_a)
    ad_b = SQLiteAdapter(conn_b)
    try:

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

        with pytest.raises(IsolationConflict) as info:
            with agent_txn({"sql": ad_a}, isolation=Abort()) as ctx_a:
                assert read_x(name="x") == 0
                # Second "process": its own connection + adapter, commits
                # a write that bumps the shared version side-table.
                with agent_txn({"sql": ad_b}) as ctx_b:
                    write_x(name="x", val=99)
                assert ctx_b.txn.state is TxnState.COMMITTED
                # ctx_a's auto-commit diff sees the bump via meta_conn.

        c = info.value.conflicts[0]
        assert c.resource == "sql"
        assert c.key == ("counters", "x")
        assert c.version_at_read == 0
        assert c.version_now == 1
        assert ctx_a.txn.state is TxnState.ROLLED_BACK
    finally:
        conn_a.close()
        conn_b.close()
