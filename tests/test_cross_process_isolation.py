"""#8 (single-host tier) — cross-process Serialize coordination.

In-process MVCC and cross-process *detection* (the shared ``_pherix_versions``
side-table read through the committed-only meta connection) already work on
``main``. What did NOT exist before this stream: cross-process *coordination*.
``JournalRegistry`` is a process-global singleton, blind to other processes, so
a ``Serialize`` commit in process A could not WAIT on an in-flight conflicting
write in process B — it degraded straight to Abort.

The mechanism added here: an in-flight txn publishes its write INTENTS — the
``(resource, key)`` pairs it plans to write — into a shared ``_pherix_intents``
SQLite side-table (a sibling file of the on-disk DB, autocommit, so the intent
is visible to other processes BEFORE the txn commits). A ``Serialize`` commit
polls that table and BLOCKS while a conflicting live intent exists, proceeding
once the intent clears (the writer committed / rolled back) or the timeout
expires (then it falls through to the committed-state diff — degrade to Abort,
the honest fallback).

The cross-process pin (:func:`test_serialize_waits_for_in_flight_write_in_another_process`)
spawns a REAL second process for B, so there is no shared Python object and no
shared in-process registry — the coordination can only flow through the shared
SQLite intent file. It would FAIL against ``main`` @ 66b3204, where A's commit
returns immediately (no wait) and B's in-flight intent is invisible.

The timing assertion is the proof: B holds its write intent open for a fixed
dwell, recording the wall-clock time it RELEASES; A records when its Serialize
commit COMPLETES. A must complete no earlier than B released — i.e. A waited.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.isolation import Serialize
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY as TOOL_REGISTRY, tool


# --- fixtures / helpers ------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tool_registry():
    """Restore the tool registry between tests so @tool inside a function
    doesn't bleed across files."""
    snapshot = dict(TOOL_REGISTRY._tools)
    yield
    TOOL_REGISTRY._tools = snapshot


@pytest.fixture
def shared_db(tmp_path: Path) -> Path:
    """A file-backed SQLite DB seeded with a counters table (WAL)."""
    db = tmp_path / "ledger.db"
    boot = sqlite3.connect(str(db), isolation_level=None)
    boot.execute("PRAGMA journal_mode=WAL")
    boot.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER)")
    boot.execute("INSERT INTO counters VALUES ('x', 0)")
    boot.close()
    return db


def _open_adapter(db: Path) -> tuple[sqlite3.Connection, SQLiteAdapter]:
    conn = sqlite3.connect(str(db), isolation_level=None)
    return conn, SQLiteAdapter(conn)


# The body run inside a real second process ("process B"). It opens an
# agent_txn, WRITES counters.x (publishing a live intent on ("counters","x")
# into the sibling intent file), signals readiness by touching a file, holds
# the txn open for ``dwell`` seconds, then rolls back (so the version of x is
# NOT bumped — A's post-wait diff stays clean and A can commit). It writes the
# wall-clock release time to ``released_path`` as its last act before exit.
_B_PROCESS_SOURCE = textwrap.dedent(
    """
    import sqlite3, sys, time
    from pathlib import Path
    from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
    from pherix.core.runtime import agent_txn
    from pherix.core.tools import tool

    db, ready_path, released_path, dwell = sys.argv[1:5]
    dwell = float(dwell)

    conn = sqlite3.connect(db, isolation_level=None)
    adapter = SQLiteAdapter(conn)

    @tool(resource="sql")
    def write_x(conn, name, val):
        execute_isolated(
            conn,
            "UPDATE counters SET val = ? WHERE name = ?",
            (val, name),
            writes=[("counters", name)],
        )

    try:
        # Roll back at the end: the write's job is only to publish the
        # in-flight intent A must wait on, not to actually move x's version.
        with agent_txn({"sql": adapter}) as ctx:
            write_x(name="x", val=123)   # publishes intent on ("counters","x")
            Path(ready_path).write_text("ready")  # signal A: intent is live
            time.sleep(dwell)            # hold the intent open
            ctx.rollback()               # finalise -> intent cleared on exit
        # Record the release instant AFTER the intent has been cleared.
        Path(released_path).write_text(repr(time.time()))
    finally:
        conn.close()
    """
)


# --- the cross-process pin ---------------------------------------------------


def test_serialize_waits_for_in_flight_write_in_another_process(
    shared_db: Path, tmp_path: Path
):
    """A's Serialize commit (this process) must WAIT on B's in-flight write
    (a real second process) and proceed only once B releases its intent.

    Proof is timing-based: A's commit completes no earlier than B released.
    Against main @ 66b3204 A would not wait at all — it has no view of B's
    in-flight plan — so A would complete promptly, well before B's release.
    """
    ready_path = tmp_path / "b_ready"
    released_path = tmp_path / "b_released"
    dwell = 0.6  # B holds its intent open for this long

    proc = subprocess.Popen(
        [sys.executable, "-c", _B_PROCESS_SOURCE,
         str(shared_db), str(ready_path), str(released_path), str(dwell)],
    )
    try:
        # Wait until B has opened its txn and published the live intent.
        deadline = time.monotonic() + 5.0
        while not ready_path.exists():
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail("process B never published its intent")
            time.sleep(0.01)

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
                row = cur.fetchone()
                return row[0] if row else None

            # A reads x (records read_keys), then commits under Serialize.
            # Serialize sees B's live intent on ("counters","x") in the shared
            # intent file and blocks until B clears it. B rolls back, so x's
            # committed version never moved -> A's post-wait diff is clean ->
            # A commits successfully.
            with agent_txn(
                {"sql": ad_a}, isolation=Serialize(timeout_seconds=5.0)
            ) as ctx_a:
                v = read_x(name="x")
                assert v == 0
                # Auto-commit on __exit__ runs the Serialize wait + diff:
                # blocks on B's live intent, then proceeds once B clears it.
        finally:
            conn_a.close()

        proc.wait(timeout=5.0)
        # B rolled back (x's committed version never moved), so A's post-wait
        # diff was clean and A committed. The duration pin below is the
        # quantitative proof that A actually waited rather than racing through.
        assert ctx_a.txn.state.name == "COMMITTED"
        assert released_path.exists()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_serialize_commit_duration_covers_the_blocking_writer(
    shared_db: Path, tmp_path: Path
):
    """Stronger timing pin: measure A's Serialize commit DURATION and assert
    it is at least most of B's dwell — i.e. A genuinely blocked on B's intent.

    Splitting the read (no wait) from the commit (the wait) isolates the wait
    cost. Against main this duration would be ~0 (no cross-process wait
    exists); here it must approach B's dwell.
    """
    ready_path = tmp_path / "b_ready"
    released_path = tmp_path / "b_released"
    dwell = 0.6

    proc = subprocess.Popen(
        [sys.executable, "-c", _B_PROCESS_SOURCE,
         str(shared_db), str(ready_path), str(released_path), str(dwell)],
    )
    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists():
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail("process B never published its intent")
            time.sleep(0.01)

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
                row = cur.fetchone()
                return row[0] if row else None

            commit_seconds = {"t": None}
            with agent_txn(
                {"sql": ad_a}, isolation=Serialize(timeout_seconds=5.0)
            ) as ctx_a:
                # B's intent was already live before A read; A reads now.
                read_x(name="x")
                # Time only the commit — that is where the Serialize wait
                # lives. Calling commit() explicitly inside the block makes
                # the auto-commit on __exit__ a no-op (ctx is _finished).
                t0 = time.monotonic()
                ctx_a.commit()
                commit_seconds["t"] = time.monotonic() - t0
            commit_seconds = commit_seconds["t"]
        finally:
            conn_a.close()

        # A's commit blocked on B's intent for most of B's dwell. Allow slack
        # for poll granularity and the small window between A's read and the
        # commit call. The key contrast: against main this is ~0.
        assert commit_seconds >= dwell * 0.5, (
            f"Serialize commit returned in {commit_seconds:.3f}s; expected to "
            f"block ~{dwell}s on B's cross-process intent"
        )
        assert ctx_a.txn.state.name == "COMMITTED"
    finally:
        if proc.poll() is None:
            proc.kill()


# --- in-process Serialize must still work (no regression) --------------------


def test_in_process_serialize_quiet_world_still_proceeds(shared_db: Path):
    """The in-process fast path is unchanged: a Serialize commit with no
    concurrent writer commits cleanly (the cross-process layer finds no
    foreign intent and returns immediately)."""
    conn, ad = _open_adapter(shared_db)
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

        with agent_txn({"sql": ad}, isolation=Serialize()) as ctx:
            read_x(name="x")
            write_x(name="x", val=7)

        assert ctx.txn.state.name == "COMMITTED"
        post = sqlite3.connect(str(shared_db)).execute(
            "SELECT val FROM counters WHERE name = 'x'"
        ).fetchone()[0]
        assert post == 7
    finally:
        conn.close()


def test_in_process_serialize_waits_for_concurrent_thread_writer(
    shared_db: Path,
):
    """In-process cross-THREAD Serialize wait still holds (the Event path).

    A reads x@0; B opens in another thread, queues a write on x, holds the
    txn open, then commits. A's Serialize commit waits on B's close before the
    diff. B's commit bumps x's version, so A's post-wait diff conflicts — but
    the pin is the WAIT: A finishes only after B does.
    """
    from pherix.core.isolation import IsolationConflict

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

        a_ready = threading.Event()
        b_started = threading.Event()
        b_finished_at: dict[str, float] = {}

        def run_b():
            # SQLite connections are thread-affine — open B's connection in
            # the thread that uses it.
            conn_b, ad_b = _open_adapter(shared_db)
            try:
                a_ready.wait(timeout=2.0)
                with agent_txn({"sql": ad_b}):
                    write_x(name="x", val=55)
                    b_started.set()
                    time.sleep(0.05)  # hold open while A's commit waits
                b_finished_at["t"] = time.monotonic()
            finally:
                conn_b.close()

        thread = threading.Thread(target=run_b)
        thread.start()

        a_finished_at: dict[str, float] = {}
        with pytest.raises(IsolationConflict):
            with agent_txn(
                {"sql": ad_a}, isolation=Serialize(timeout_seconds=3.0)
            ):
                read_x(name="x")
                a_ready.set()
                b_started.wait(timeout=2.0)
                # commit on __exit__ waits on B (in-process Event), then the
                # post-wait diff sees x bumped -> IsolationConflict.

        a_finished_at["t"] = time.monotonic()
        thread.join()
        assert a_finished_at["t"] >= b_finished_at["t"]
    finally:
        conn_a.close()
