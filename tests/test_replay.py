"""Slice 5 — replay the journal forward against fresh state.

Pins:

- verify-mode equality is the algorithmic heart; the default comparator is
  ``strict_json_default``-canonicalised string equality.
- a tampered audit row diverges; a tolerant per-tool comparator hides
  legitimate run-to-run variation.
- irreversible-APPLIED effects are never re-fired (the journal is the
  witness for resources Pherix cannot honestly snapshot — retires the
  Slice-3 "idempotency test is a pin, not a scenario" follow-up).
- cross-resource journals (SQL + FS + HTTP) round-trip cleanly, including
  byte payloads.
- isolation read/write triples (Slice 4) survive the round-trip without
  flagging false conflicts.
- the replay txn carries ``replayed_from`` and its own journal lands in
  ``target_audit``.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from pherix import (
    AuditJournal,
    FilesystemAdapter,
    HTTPAdapter,
    Policy,
    PolicyViolation,
    ReplayDivergence,
    ReplayResult,
    SQLiteAdapter,
    agent_txn,
    replay,
    tool,
)
from pherix.core.adapters.filesystem import FsHandle
from pherix.core.adapters.sql import execute_isolated


# --- fixtures ---------------------------------------------------------------


def _fresh_users_db():
    """Return a connection with the standard ``users`` table — autocommit mode.

    The runtime's adapter expects ``isolation_level=None`` so the adapter
    (not sqlite3's implicit machinery) drives every BEGIN / COMMIT.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    return conn


def _names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM users ORDER BY id")]


# --- verify happy path ------------------------------------------------------


def test_verify_mode_returns_success_for_deterministic_sql_journal(tmp_path):
    """A SQL-only journal of inserts replays clean — every effect's recorded
    result equals the replayed result under the default JSON comparator."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    audit_path = str(tmp_path / "audit.db")
    source_audit = AuditJournal(audit_path)

    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
        insert_user(name="bob")
    src_txn_id = ctx.txn_id

    fresh_conn = _fresh_users_db()
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh_conn)},
        source_audit=source_audit,
    )
    assert result.status == "success"
    assert result.mode == "verify"
    assert result.source_txn_id == src_txn_id
    assert result.replay_txn_id != src_txn_id
    assert len(result.outcomes) == 2
    assert all(o.status == "match" for o in result.outcomes)
    assert result.divergences == []
    # Fresh DB carries the replayed state on success.
    assert _names(fresh_conn) == ["alice", "bob"]
    source_audit.close()


def test_verify_records_replay_txn_in_target_audit_with_replayed_from(tmp_path):
    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    target_audit = AuditJournal(str(tmp_path / "target.db"))

    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
    src_txn_id = ctx.txn_id

    fresh_conn = _fresh_users_db()
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh_conn)},
        source_audit=source_audit,
        target_audit=target_audit,
    )
    row = target_audit.get_transaction(result.replay_txn_id)
    assert row is not None
    assert row["replayed_from"] == src_txn_id
    assert row["state"] == "COMMITTED"
    # Every replayed effect produced a row in target_audit.
    rows = target_audit.get_effects(result.replay_txn_id)
    assert [r["tool"] for r in rows] == ["insert_user"]
    source_audit.close()
    target_audit.close()


# --- divergence -------------------------------------------------------------


def test_verify_flags_tampered_result_as_divergence_and_raises(tmp_path):
    """A manually edited audit row produces a result the replayed tool
    cannot match — verify catches it and raises ReplayDivergence by default."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    audit_path = str(tmp_path / "audit.db")
    source_audit = AuditJournal(audit_path)

    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
    src_txn_id = ctx.txn_id
    source_audit.close()

    # Tamper directly with the audit DB — operator's "manually-edited result row"
    # scenario from the slice spec.
    raw = sqlite3.connect(audit_path)
    raw.execute(
        "UPDATE effects SET result = ? WHERE txn_id = ?",
        (json.dumps("MALLORY"), src_txn_id),
    )
    raw.commit()
    raw.close()

    source_audit = AuditJournal(audit_path)
    fresh_conn = _fresh_users_db()
    with pytest.raises(ReplayDivergence) as exc:
        replay(
            src_txn_id,
            {"sql": SQLiteAdapter(fresh_conn)},
            source_audit=source_audit,
        )
    result: ReplayResult = exc.value.result
    assert result.status == "divergence"
    assert len(result.divergences) == 1
    div = result.divergences[0]
    assert div.recorded_result == "MALLORY"
    assert div.replayed_result == "alice"
    # The replay txn rolled back — fresh DB is empty after a divergence.
    assert _names(fresh_conn) == []
    source_audit.close()


def test_divergence_restores_fs_state_on_rollback(tmp_path):
    """Cross-resource divergence: SQL ROLLBACK discards the BEGIN bracket
    (savepoints become moot), but FilesystemAdapter.rollback() only deletes
    its backup tempdir — restoring file state needs a per-effect
    adapter.restore(snapshot) walk newest-first, same shape as the
    runtime's normal rollback. Without that walk, the FS writes the
    replay just produced linger on the fresh root.
    """

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    @tool(resource="fs")
    def store_doc(fs: FsHandle, name: str, body: bytes):
        fs.write(f"{name}.txt", body)
        return name

    audit_path = str(tmp_path / "audit.db")
    source_audit = AuditJournal(audit_path)

    src_conn = _fresh_users_db()
    src_fs_root = Path(tempfile.mkdtemp(prefix="pherix_src_"))
    src_adapters = {
        "sql": SQLiteAdapter(src_conn),
        "fs": FilesystemAdapter(src_fs_root),
    }
    with agent_txn(src_adapters, audit=source_audit) as ctx:
        insert_user(name="alice")
        store_doc(name="alice", body=b"hello")
    src_txn_id = ctx.txn_id
    source_audit.close()

    # Tamper the SQL effect result so replay verify diverges AFTER the
    # FS effect has already been applied to the fresh root.
    raw = sqlite3.connect(audit_path)
    raw.execute(
        "UPDATE effects SET result = ? WHERE txn_id = ? AND idx = 0",
        (json.dumps("MALLORY"), src_txn_id),
    )
    raw.commit()
    raw.close()

    source_audit = AuditJournal(audit_path)
    fresh_conn = _fresh_users_db()
    fresh_fs_root = Path(tempfile.mkdtemp(prefix="pherix_fresh_"))
    with pytest.raises(ReplayDivergence):
        replay(
            src_txn_id,
            {
                "sql": SQLiteAdapter(fresh_conn),
                "fs": FilesystemAdapter(fresh_fs_root),
            },
            source_audit=source_audit,
        )
    # SQL is restored by the adapter-level ROLLBACK.
    assert _names(fresh_conn) == []
    # FS must also be restored — without per-effect restore on rollback,
    # the file replay just wrote would persist on the fresh root.
    assert list(fresh_fs_root.iterdir()) == []
    source_audit.close()


def test_verify_with_raise_on_divergence_false_returns_result(tmp_path):
    """``raise_on_divergence=False`` collects divergences into the result
    object instead of raising — the operator can branch on outcome shape."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    audit_path = str(tmp_path / "audit.db")
    source_audit = AuditJournal(audit_path)
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
    src_txn_id = ctx.txn_id
    source_audit.close()

    raw = sqlite3.connect(audit_path)
    raw.execute(
        "UPDATE effects SET result = ? WHERE txn_id = ?",
        (json.dumps("tampered"), src_txn_id),
    )
    raw.commit()
    raw.close()

    source_audit = AuditJournal(audit_path)
    fresh_conn = _fresh_users_db()
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh_conn)},
        source_audit=source_audit,
        raise_on_divergence=False,
    )
    assert result.status == "divergence"
    assert len(result.divergences) == 1
    source_audit.close()


# --- comparator escape hatch -----------------------------------------------


def test_custom_comparator_passes_where_default_would_diverge(tmp_path):
    """``@tool(comparator=fn)`` relaxes equality for tools whose recorded
    result legitimately varies between runs."""

    counter = {"n": 0}

    def ignore_value(_recorded, _replayed):
        # Trivial: always equal. Real comparators (e.g. ignore_seconds)
        # would compare with tolerance; this is enough to pin the wiring.
        return True

    @tool(resource="sql", comparator=ignore_value)
    def generate_id(conn, prefix):
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        generate_id(prefix="x")  # source: "x-1"
    src_txn_id = ctx.txn_id

    # Replay re-fires and yields "x-2" — the default JSON comparator would
    # diverge; the per-tool comparator passes.
    fresh_conn = _fresh_users_db()
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh_conn)},
        source_audit=source_audit,
    )
    assert result.status == "success"
    source_audit.close()


def test_default_comparator_diverges_when_recorded_result_varies(tmp_path):
    """Without a custom comparator, a result that drifts between runs is
    flagged as divergence — the slice's TDD-first pin."""

    counter = {"n": 0}

    @tool(resource="sql")
    def time_now(conn):
        counter["n"] += 1
        return {"call": counter["n"]}

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        time_now()  # records {"call": 1}
    src_txn_id = ctx.txn_id

    fresh_conn = _fresh_users_db()
    with pytest.raises(ReplayDivergence) as exc:
        replay(
            src_txn_id,
            {"sql": SQLiteAdapter(fresh_conn)},
            source_audit=source_audit,
        )
    assert len(exc.value.result.divergences) == 1
    source_audit.close()


# --- reconstruct mode -------------------------------------------------------


def test_reconstruct_rebuilds_sql_state_on_fresh_db(tmp_path):
    """Reconstruct mode walks the journal, accepts whatever today's apply
    produces, and commits — the operator's disaster-recovery story."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
        insert_user(name="bob")
        insert_user(name="carol")
    src_txn_id = ctx.txn_id

    fresh_conn = _fresh_users_db()
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh_conn)},
        source_audit=source_audit,
        mode="reconstruct",
    )
    assert result.status == "success"
    assert result.mode == "reconstruct"
    # The world matches what the source journal described.
    assert _names(fresh_conn) == ["alice", "bob", "carol"]
    assert all(o.status == "applied" for o in result.outcomes)
    source_audit.close()


# --- irreversible skip-and-reuse (retires Slice-3 follow-up) ---------------


def test_irreversible_applied_is_never_refired_on_replay(tmp_path):
    """Source-status APPLIED + reversible=False (HTTP) → replay skips and
    reuses the recorded result. No fresh call. This is what ``effect_id``
    idempotency was built for; retires the Slice-3 follow-up that flagged
    'idempotency test is a pin, not a scenario'."""

    fired: list[str] = []

    @tool(resource="http", reversible=False, injects_handle=False, name="ping")
    def ping(url):
        fired.append(url)
        return {"status": 200, "url": url}

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    with agent_txn({"http": HTTPAdapter()}, audit=source_audit) as ctx:
        r = ping(url="https://example.com")
        ctx.approve_irreversible(r.effect_id)
    src_txn_id = ctx.txn_id
    assert fired == ["https://example.com"]  # source fired once

    # Replay — same HTTPAdapter doesn't matter; it would never be called.
    result = replay(
        src_txn_id,
        {"http": HTTPAdapter()},
        source_audit=source_audit,
        mode="verify",
    )
    assert result.status == "success"
    # Counter unchanged: replay reused the journal's recorded result.
    assert fired == ["https://example.com"]
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "skipped_idempotent"
    source_audit.close()


def test_reconstruct_also_skips_already_applied_irreversibles(tmp_path):
    """Reconstruct re-fires reversibles to rebuild state but must NOT
    re-fire APPLIED irreversibles — double-billing the world is exactly the
    bug Pherix's irreversible lane exists to prevent."""

    fired: list[str] = []

    @tool(resource="http", reversible=False, injects_handle=False, name="ping")
    def ping(url):
        fired.append(url)
        return {"status": 200}

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    with agent_txn({"http": HTTPAdapter()}, audit=source_audit) as ctx:
        r = ping(url="https://example.com")
        ctx.approve_irreversible(r.effect_id)
    src_txn_id = ctx.txn_id

    result = replay(
        src_txn_id,
        {"http": HTTPAdapter()},
        source_audit=source_audit,
        mode="reconstruct",
    )
    assert result.status == "success"
    assert fired == ["https://example.com"]  # not "https://example.com" twice


# --- cross-resource (SQL + FS + HTTP) --------------------------------------


def test_cross_resource_journal_replays_clean_under_both_modes(tmp_path):
    """A journal mixing SQL inserts, FS writes, and an HTTP fire replays
    cleanly under verify and reconstruct. Bytes payloads round-trip through
    the audit row's strict-JSON encoding back to ``bytes``."""

    fired: list[str] = []

    @tool(resource="sql")
    def insert_doc(conn, doc_id, title):
        conn.execute("INSERT INTO docs (id, title) VALUES (?, ?)", (doc_id, title))
        return doc_id

    @tool(resource="fs")
    def store_doc(fs: FsHandle, doc_id, body: bytes):
        fs.write(f"{doc_id}.txt", body)
        return doc_id

    @tool(resource="http", reversible=False, injects_handle=False, name="notify")
    def notify(doc_id):
        fired.append(doc_id)
        return {"status": "ok"}

    # --- source run ---
    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = sqlite3.connect(":memory:", isolation_level=None)
    src_conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    src_fs_root = Path(tempfile.mkdtemp(prefix="pherix_src_"))

    adapters_src = {
        "sql": SQLiteAdapter(src_conn),
        "fs": FilesystemAdapter(src_fs_root),
        "http": HTTPAdapter(),
    }
    with agent_txn(adapters_src, audit=source_audit) as ctx:
        insert_doc(doc_id="d1", title="Intro")
        store_doc(doc_id="d1", body=b"# Intro\nhello")
        r = notify(doc_id="d1")
        ctx.approve_irreversible(r.effect_id)
    src_txn_id = ctx.txn_id
    assert fired == ["d1"]

    # --- verify against truly-fresh adapters ---
    fresh_conn = sqlite3.connect(":memory:", isolation_level=None)
    fresh_conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    fresh_fs_root = Path(tempfile.mkdtemp(prefix="pherix_verify_"))
    result = replay(
        src_txn_id,
        {
            "sql": SQLiteAdapter(fresh_conn),
            "fs": FilesystemAdapter(fresh_fs_root),
            "http": HTTPAdapter(),
        },
        source_audit=source_audit,
        mode="verify",
    )
    assert result.status == "success"
    assert fired == ["d1"]  # http effect skipped on replay

    # SQL row is there; FS file contents match the recorded bytes.
    assert fresh_conn.execute("SELECT title FROM docs WHERE id='d1'").fetchone() == ("Intro",)
    assert (fresh_fs_root / "d1.txt").read_bytes() == b"# Intro\nhello"

    # --- reconstruct on yet-another fresh substrate ---
    fresh2_conn = sqlite3.connect(":memory:", isolation_level=None)
    fresh2_conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    fresh2_fs_root = Path(tempfile.mkdtemp(prefix="pherix_recon_"))
    result2 = replay(
        src_txn_id,
        {
            "sql": SQLiteAdapter(fresh2_conn),
            "fs": FilesystemAdapter(fresh2_fs_root),
            "http": HTTPAdapter(),
        },
        source_audit=source_audit,
        mode="reconstruct",
    )
    assert result2.status == "success"
    assert (fresh2_fs_root / "d1.txt").read_bytes() == b"# Intro\nhello"
    assert fired == ["d1"]
    source_audit.close()


# --- isolation read/write keys round-trip ----------------------------------


def test_isolation_keys_round_trip_without_false_conflicts(tmp_path):
    """A journal whose original commit fired ``check_conflicts`` cleanly
    replays without flagging false conflicts. The P2/P3 work — persisting
    read_keys/write_keys triples — is what makes this honest."""

    @tool(resource="sql")
    def bump(conn, who):
        # Declare the row this statement touches so the runtime captures
        # read/write keys via Slice 4's ``execute_isolated`` helper.
        execute_isolated(
            conn,
            "INSERT INTO users (name) VALUES (?)",
            (who,),
            writes=[("users", who)],
        )
        return who

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        bump(who="alice")
        bump(who="bob")
    src_txn_id = ctx.txn_id

    # Verify replay — fresh DB, fresh adapter. No concurrent writer; the
    # commit-time diff on the replay txn should find no conflicts.
    fresh_conn = _fresh_users_db()
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh_conn)},
        source_audit=source_audit,
    )
    assert result.status == "success"
    assert result.isolation_conflicts == []
    source_audit.close()


# --- input validation ------------------------------------------------------


def test_replay_unknown_txn_id_raises(tmp_path):
    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    with pytest.raises(ValueError, match="no effects found"):
        replay(
            "txn-doesnotexist",
            {},
            source_audit=source_audit,
        )
    source_audit.close()


def test_replay_invalid_mode_raises():
    audit = AuditJournal.in_memory()
    with pytest.raises(ValueError, match="mode must be"):
        replay("txn-x", {}, source_audit=audit, mode="garbage")  # type: ignore[arg-type]
    audit.close()


def test_replay_propagates_policy_denial(tmp_path):
    """Replay re-evaluates policy at stage-time (Slice 1 D6 symmetry). If
    the operator hands replay a stricter policy than the source ran under,
    the denial fails replay loudly rather than firing tools off-policy."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
    src_txn_id = ctx.txn_id

    fresh_conn = _fresh_users_db()
    deny = Policy(deny={"insert_user"})
    with pytest.raises(PolicyViolation):
        replay(
            src_txn_id,
            {"sql": SQLiteAdapter(fresh_conn)},
            source_audit=source_audit,
            policy=deny,
        )
    source_audit.close()


def test_stricter_policy_does_not_block_irreversible_skip(tmp_path):
    """Irreversibles are skip-and-reused on replay; nothing fires. A stricter
    policy at replay-time would block re-firing, but the skip path never
    invokes ``policy.check`` — pins the contract that policy gates re-fire,
    not journal-walk."""

    @tool(resource="http", reversible=False, injects_handle=False, name="ping")
    def ping(url):
        return {"status": 200}

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    with agent_txn({"http": HTTPAdapter()}, audit=source_audit) as ctx:
        r = ping(url="https://example.com")
        ctx.approve_irreversible(r.effect_id)
    src_txn_id = ctx.txn_id

    # Replay under a policy that would deny ping if it tried to re-fire.
    deny = Policy(deny={"ping"})
    result = replay(
        src_txn_id,
        {"http": HTTPAdapter()},
        source_audit=source_audit,
        policy=deny,
    )
    assert result.status == "success"
    assert result.outcomes[0].status == "skipped_idempotent"
    source_audit.close()


def test_replay_missing_tool_registration_gives_friendly_error(tmp_path):
    """A bare ``KeyError`` from a registry miss is opaque. Replay wraps it so
    the operator sees "tool X not registered in this process; replay needs
    the same @tool defs as the source" instead of an unhelpful traceback."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
    src_txn_id = ctx.txn_id

    # Simulate "operator forgot to register the tool in the replay process"
    # by clearing the registry between source and replay.
    from pherix.core.tools import REGISTRY as TR
    TR.clear()

    fresh_conn = _fresh_users_db()
    with pytest.raises(RuntimeError, match="not registered in this process"):
        replay(
            src_txn_id,
            {"sql": SQLiteAdapter(fresh_conn)},
            source_audit=source_audit,
        )
    source_audit.close()


def test_replay_missing_adapter_raises(tmp_path):
    """Source journal references a resource the operator forgot to supply →
    replay raises before any side effects fire."""

    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    source_audit = AuditJournal(str(tmp_path / "audit.db"))
    src_conn = _fresh_users_db()
    with agent_txn({"sql": SQLiteAdapter(src_conn)}, audit=source_audit) as ctx:
        insert_user(name="alice")
    src_txn_id = ctx.txn_id

    with pytest.raises(RuntimeError, match="no adapter provided for resource 'sql'"):
        replay(src_txn_id, {}, source_audit=source_audit)
    source_audit.close()
