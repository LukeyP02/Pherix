"""Slice 4 acceptance — the cross-stream + cross-resource + cross-process pins.

Stream C pinned the algorithm against a hand-written :class:`FakeAdapter`.
This file pins the same behaviour through the *real* :class:`SQLiteAdapter`
and :class:`FilesystemAdapter` plus the :class:`HTTPAdapter` staging lane:
the end-to-end story the TASK's Done-when criteria actually demand.

Scenarios:

  * Lost-update through real SQL — Abort, Retry, Serialize.
  * In-process arbitration: two ``agent_txn`` blocks in the same Python
    process conflicting on a shared SQL row.
  * Cross-resource conflict: a single txn that reads SQL + writes FS +
    stages an HTTP charge, conflicting with another committed txn on the
    SQL row. The irreversible (HTTP) staged effect never fires, the FS
    write is rolled back, and the SQL read is what triggers the policy.
  * Filesystem-shared SQLite journal cross-process: two Python
    subprocesses against the same on-disk SQLite file — the second
    process's commit-time diff sees the first's bump via the meta
    connection and the resolution policy fires.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.isolation import (
    Abort,
    IsolationConflict,
    Retry,
    Serialize,
)
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY as TOOL_REGISTRY, tool
from pherix.core.transaction import TxnState
from pherix.frontends.library import run_txn


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tool_registry():
    """Restore the tool registry between tests so @tool inside a fixture
    doesn't bleed across files. Tests in this module register fresh tools
    in each function; resetting keeps the suite self-contained.
    """
    snapshot = dict(TOOL_REGISTRY._tools)
    yield
    TOOL_REGISTRY._tools = snapshot


@pytest.fixture
def shared_db(tmp_path: Path) -> Path:
    """A file-backed SQLite DB seeded with a counters table."""
    db = tmp_path / "shared.db"
    bootstrap = sqlite3.connect(str(db), isolation_level=None)
    bootstrap.execute("PRAGMA journal_mode=WAL")
    bootstrap.execute(
        "CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER)"
    )
    bootstrap.execute("INSERT INTO counters VALUES ('x', 0)")
    bootstrap.close()
    return db


def _open_adapter(db: Path) -> tuple[sqlite3.Connection, SQLiteAdapter]:
    conn = sqlite3.connect(str(db), isolation_level=None)
    return conn, SQLiteAdapter(conn)


# --- lost-update through real SQL -------------------------------------------


def test_lost_update_real_sql_under_abort(shared_db: Path):
    """Two SQL adapters on the same file. A reads x at v0; B writes x and
    commits (bumps version to 1); A's commit diff fires IsolationConflict.
    """
    conn_a, ad_a = _open_adapter(shared_db)
    conn_b, ad_b = _open_adapter(shared_db)
    try:

        @tool(resource="sql")
        def read_x(conn, name):
            cur = execute_isolated(
                conn,
                "SELECT val FROM counters WHERE name = ?",
                (name,),
                reads=[("counters", name)],
            )
            row = cur.fetchone()
            return row[0] if row else None

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
                v = read_x(name="x")
                assert v == 0
                # B opens, writes, commits — version bumped to 1 globally.
                with agent_txn({"sql": ad_b}) as ctx_b:
                    write_x(name="x", val=99)
                # A's auto-commit: diff sees v_read=0, v_now=1 via meta_conn.

        c = info.value.conflicts[0]
        assert c.resource == "sql"
        assert c.key == ("counters", "x")
        assert c.version_at_read == 0
        assert c.version_now == 1
        # B's write survives; A rolled back cleanly.
        assert ctx_b.txn.state is TxnState.COMMITTED
        # A's reversible journal had no writes; nothing to undo on the
        # data table, but ctx_a's state must reflect the rollback.
        assert ctx_a.txn.state is TxnState.ROLLED_BACK
        post = sqlite3.connect(str(shared_db)).execute(
            "SELECT val FROM counters WHERE name = 'x'"
        ).fetchone()[0]
        assert post == 99
    finally:
        conn_a.close()
        conn_b.close()


def test_lost_update_real_sql_under_retry_replays_and_succeeds(
    shared_db: Path,
):
    """run_txn(fn, isolation=Retry(2)) replays after a conflict. First
    attempt: A reads x@0, B commits (v→1), A's commit conflicts → rollback
    + retry. Second attempt: A reads x@1, no concurrent writer this time,
    succeeds.
    """
    conn_a, ad_a = _open_adapter(shared_db)
    conn_b, ad_b = _open_adapter(shared_db)
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

        attempts = {"n": 0}
        reads_seen: list[int] = []

        def body(ctx):
            attempts["n"] += 1
            reads_seen.append(read_x(name="x"))
            if attempts["n"] == 1:
                # On the first attempt only, run a concurrent committed
                # writer. The retry sees the post-B world and goes
                # through without interference.
                with agent_txn({"sql": ad_b}) as _:
                    write_x(name="x", val=42)

        run_txn(body, {"sql": ad_a}, isolation=Retry(max_attempts=3))

        assert attempts["n"] == 2
        assert reads_seen == [0, 42]
    finally:
        conn_a.close()
        conn_b.close()


def test_lost_update_real_sql_retry_exhaustion(shared_db: Path):
    """Retry(1) with a body that always conflicts: raises IsolationConflict
    after one attempt."""
    conn_a, ad_a = _open_adapter(shared_db)
    conn_b, ad_b = _open_adapter(shared_db)
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

        bumps = {"n": 0}

        def body(ctx):
            read_x(name="x")
            bumps["n"] += 1
            # Always run an external writer mid-body so every attempt
            # conflicts.
            with agent_txn({"sql": ad_b}) as _:
                write_x(name="x", val=bumps["n"])

        with pytest.raises(IsolationConflict):
            run_txn(body, {"sql": ad_a}, isolation=Retry(max_attempts=2))

        assert bumps["n"] == 2  # both attempts ran
    finally:
        conn_a.close()
        conn_b.close()


def test_serialize_with_real_sql_is_quiet_world_proceeds(shared_db: Path):
    """Serialize with no concurrent in-flight writer: the wait returns
    immediately and the diff is clean. A's commit succeeds."""
    conn_a, ad_a = _open_adapter(shared_db)
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

        with agent_txn(
            {"sql": ad_a},
            isolation=Serialize(timeout_seconds=2.0),
        ) as ctx:
            assert read_x(name="x") == 0
        assert ctx.txn.state is TxnState.COMMITTED
    finally:
        conn_a.close()


# --- in-process arbitration (single-process, two nested txns) ---------------


def test_in_process_arbitration_two_nested_agent_txn(shared_db: Path):
    """Two ``agent_txn`` blocks in one process, two adapters on the same
    file. The inner txn's commit bumps the version side-table; the outer
    txn's later commit diff sees the move and fires IsolationConflict.

    This is the in-process arbitration story: D5 says the registry +
    shared adapter state is the single-process arbiter; this test
    confirms the algorithm runs through real adapters in one process.
    """
    conn_outer, ad_outer = _open_adapter(shared_db)
    conn_inner, ad_inner = _open_adapter(shared_db)
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

        with pytest.raises(IsolationConflict):
            with agent_txn({"sql": ad_outer}, isolation=Abort()) as outer:
                read_x(name="x")
                with agent_txn({"sql": ad_inner}) as inner:
                    write_x(name="x", val=7)
                # outer auto-commit: diff fires.
    finally:
        conn_outer.close()
        conn_inner.close()


# --- cross-resource conflict (SQL + FS + HTTP) ------------------------------


def test_cross_resource_conflict_sql_fs_http(shared_db: Path, tmp_path: Path):
    """The mixed-resource pin from TASK.md's Done-when:

        a txn that reads from a SQL row, writes a file, and stages an
        HTTP charge, conflicting on the SQL row read with another
        committed txn.

    The HTTP staged effect never fires (commit aborts before the staging
    fire loop); the FS write is rolled back via the per-effect backup;
    the SQL conflict is what triggers IsolationConflict.
    """
    conn_a, ad_a = _open_adapter(shared_db)
    conn_b, ad_b = _open_adapter(shared_db)
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    fs_adapter = FilesystemAdapter(fs_root)
    http_adapter = HTTPAdapter()
    fired_http: list[dict] = []

    try:

        @tool(resource="sql")
        def read_balance(conn, account):
            cur = execute_isolated(
                conn,
                "SELECT val FROM counters WHERE name = ?",
                (account,),
                reads=[("counters", account)],
            )
            return cur.fetchone()[0]

        @tool(resource="sql")
        def credit(conn, account, amount):
            execute_isolated(
                conn,
                "UPDATE counters SET val = val + ? WHERE name = ?",
                (amount, account),
                writes=[("counters", account)],
            )

        @tool(resource="fs")
        def write_receipt(handle, path, body):
            handle.write(path, body.encode("utf-8"))

        @tool(resource="http", reversible=False, injects_handle=False)
        def charge_card(account, amount):
            fired_http.append({"account": account, "amount": amount})

        @tool(resource="http", reversible=False, injects_handle=False)
        def refund_card(account, amount):
            fired_http.append({"refund": True, "account": account, "amount": amount})

        # Register refund_card as charge_card's compensator so the gate
        # doesn't block the test on missing approval — this isn't a gate
        # test, it's an isolation test.
        @tool(
            resource="http",
            reversible=False,
            injects_handle=False,
            compensator="refund_card",
        )
        def charge_card_compensable(account, amount):
            fired_http.append({"account": account, "amount": amount})

        adapters = {"sql": ad_a, "fs": fs_adapter, "http": http_adapter}

        with pytest.raises(IsolationConflict) as info:
            with agent_txn(adapters, isolation=Abort()) as ctx_a:
                bal = read_balance(account="x")
                assert bal == 0
                write_receipt(path="receipt.txt", body=f"old balance: {bal}")
                # Stage the irreversible — not approved yet, but with a
                # compensator so the gate passes.
                charge_card_compensable(account="x", amount=50)

                # Concurrent committed write to the SQL row.
                with agent_txn({"sql": ad_b}) as ctx_b:
                    credit(account="x", amount=999)

        # The conflict is on the SQL read, not on the FS write or the
        # HTTP stage (HTTP doesn't participate in MVCC — its adapter is
        # non-rollback and is skipped).
        keys = [c.key for c in info.value.conflicts]
        assert keys == [("counters", "x")]

        # HTTP never fired — staged effect was unwound before the fire
        # loop reached it.
        assert fired_http == []

        # FS receipt rolled back — file should not exist.
        assert not (fs_root / "receipt.txt").exists()

        # SQL row reflects only B's committed credit.
        post = sqlite3.connect(str(shared_db)).execute(
            "SELECT val FROM counters WHERE name = 'x'"
        ).fetchone()[0]
        assert post == 999
    finally:
        conn_a.close()
        conn_b.close()


# --- cross-process arbitration via the filesystem-shared SQLite journal -----


_CHILD_PROC_SCRIPT = textwrap.dedent("""
    import sqlite3, sys, json
    db_path, op, account, amount = sys.argv[1:]
    amount = int(amount)
    from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
    from pherix.core.runtime import agent_txn
    from pherix.core.tools import tool

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    adapter = SQLiteAdapter(conn)

    @tool(resource="sql")
    def credit(conn, account, amount):
        execute_isolated(
            conn,
            "UPDATE counters SET val = val + ? WHERE name = ?",
            (amount, account),
            writes=[("counters", account)],
        )

    with agent_txn({"sql": adapter}) as ctx:
        credit(account=account, amount=amount)

    print(json.dumps({"txn_id": ctx.txn_id, "state": ctx.txn.state.name}))
""")


def test_cross_process_lost_update_via_shared_sqlite_journal(
    shared_db: Path, tmp_path: Path
):
    """Two Python processes, one on-disk SQLite file. Parent opens
    ``agent_txn`` and reads x; subprocess opens its own ``agent_txn``
    and credits x; parent's commit-time diff sees the cross-process
    bump via the meta-connection and IsolationConflict fires.

    This is the D5 filesystem-shared-journal acceptance bar — the diff
    fires *across processes*, not just within one.
    """
    conn, adapter = _open_adapter(shared_db)
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

        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)}

        with pytest.raises(IsolationConflict) as info:
            with agent_txn({"sql": adapter}, isolation=Abort()) as ctx:
                assert read_x(name="x") == 0
                # Subprocess opens its own agent_txn, writes, commits.
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        _CHILD_PROC_SCRIPT,
                        str(shared_db),
                        "credit",
                        "x",
                        "1000",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                )
                if result.returncode != 0:
                    pytest.fail(
                        f"child process failed:\n"
                        f"stdout: {result.stdout}\n"
                        f"stderr: {result.stderr}"
                    )
                child_report = json.loads(result.stdout.strip().split("\n")[-1])
                assert child_report["state"] == "COMMITTED"

        # The conflict is on the SQL row that the child credited.
        assert len(info.value.conflicts) == 1
        c = info.value.conflicts[0]
        assert c.resource == "sql"
        assert c.key == ("counters", "x")
        # The child bumped from v0 to v1 (one write). Parent saw v0 at
        # read; meta_conn sees v1 at commit.
        assert c.version_at_read == 0
        assert c.version_now == 1
    finally:
        conn.close()


# --- audit journal carries read/write keys ----------------------------------


def test_audit_journal_records_isolation_keys(shared_db: Path):
    """The audit journal sees read_keys / write_keys persisted with each
    effect, so the post-mortem trace of an isolation event is complete.
    """
    conn, adapter = _open_adapter(shared_db)
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

        audit = AuditJournal.in_memory()
        with agent_txn({"sql": adapter}, audit=audit) as ctx:
            read_x(name="x")

        effects = audit.get_effects(ctx.txn_id)
        # Audit row carries enough info to reconstruct the read-set.
        # ``args`` for read_x captures the name; the journal's read_keys
        # live on the in-memory Effect object — pin both shapes.
        assert effects[0]["tool"] == "read_x"
        assert json.loads(effects[0]["args"]) == {"name": "x"}
        assert ctx.txn.effects[0].read_keys == [
            ("sql", ("counters", "x"), 0),
        ]
    finally:
        conn.close()
