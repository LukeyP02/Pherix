"""The per-effect ``actor`` field — on-whose-authority provenance.

``actor`` is the principal an effect runs *on behalf of* (e.g. ``"alice"``,
``"role:admin"``), distinct from ``client_id`` (which agent/session *produced*
the effect). It is *attribution, not identity*: Pherix records the claimed
principal and never verifies it.

Capture grain: a transaction-level default set on ``agent_txn(.., actor=...)``
that every effect inherits, plus a per-call override via
:func:`pherix.acting_as` (one agent acting for several principals across calls
in the same transaction). It is held in the ``active_actor`` contextvar exactly
as ``active_effect`` holds the read/write-key recording target — the runtime
seeds it; each Effect stamps the live value as it is journalled.

These tests cover: the default flows to every effect; the per-call override
works; the actor persists and reads back through the inspector; the read path
is **NULL-tolerant** against a journal written before the column existed
(degrades to ``None``, never crashes); and a policy rule can branch on the
principal.

Tools are defined *inside* each test: the autouse ``_clean_tool_registry``
fixture clears the process-global registry around every test, so a
module-level ``@tool`` would be wiped before the test body runs (same reason as
``test_client_id.py``).
"""

from __future__ import annotations

import sqlite3

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.dry_run import dry_run
from pherix.core.policy import Allow, Deny, Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.tools import acting_as, tool
from pherix.inspector.reader import JournalReader


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def _register_insert():
    @tool(resource="sql", reversible=True)
    def insert_widget(conn: sqlite3.Connection, name: str) -> None:
        conn.execute("INSERT INTO widgets (name) VALUES (?)", (name,))

    return insert_widget


# --- the txn-level default flows to every effect ---------------------------


def test_library_caller_writes_null_actor(conn: sqlite3.Connection) -> None:
    """No ``actor`` declared → every effect's actor column is NULL."""
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as ctx:
        insert_widget(name="a")
        txn_id = ctx.txn_id
    effects = audit.get_effects(txn_id)
    assert len(effects) == 1
    assert effects[0]["actor"] is None


def test_actor_default_flows_to_every_effect(conn: sqlite3.Connection) -> None:
    """``actor`` set once on ``agent_txn`` stamps onto every effect in the txn."""
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, actor="alice"
    ) as ctx:
        insert_widget(name="a")
        insert_widget(name="b")
        insert_widget(name="c")
        txn_id = ctx.txn_id
    effects = audit.get_effects(txn_id)
    assert len(effects) == 3
    assert [e["actor"] for e in effects] == ["alice", "alice", "alice"]


def test_actor_lands_on_the_live_effect_object(conn: sqlite3.Connection) -> None:
    """The actor is on the in-memory Effect (not only the persisted row)."""
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, actor="role:admin"
    ) as ctx:
        insert_widget(name="a")
        assert ctx.txn.effects[0].actor == "role:admin"


# --- per-call override (acting_as) -----------------------------------------


def test_acting_as_overrides_per_call(conn: sqlite3.Connection) -> None:
    """One agent, one transaction, several principals across calls."""
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, actor="system"
    ) as ctx:
        insert_widget(name="a")               # inherits txn default → "system"
        with acting_as("alice"):
            insert_widget(name="b")           # override → "alice"
        insert_widget(name="c")               # back to txn default → "system"
        txn_id = ctx.txn_id
    effects = audit.get_effects(txn_id)
    assert [e["actor"] for e in effects] == ["system", "alice", "system"]


def test_acting_as_without_a_txn_default(conn: sqlite3.Connection) -> None:
    """``acting_as`` is the *only* actor source when no txn default is set."""
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as ctx:
        with acting_as("bob"):
            insert_widget(name="a")
        insert_widget(name="b")               # no override, no default → NULL
        txn_id = ctx.txn_id
    effects = audit.get_effects(txn_id)
    assert [e["actor"] for e in effects] == ["bob", None]


def test_acting_as_nests(conn: sqlite3.Connection) -> None:
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, actor="outer"
    ) as ctx:
        with acting_as("mid"):
            insert_widget(name="a")           # "mid"
            with acting_as("inner"):
                insert_widget(name="b")       # "inner"
            insert_widget(name="c")           # "mid" again
        insert_widget(name="d")               # "outer" again
        txn_id = ctx.txn_id
    effects = audit.get_effects(txn_id)
    assert [e["actor"] for e in effects] == ["mid", "inner", "mid", "outer"]


def test_dry_run_round_trips_actor(conn: sqlite3.Connection) -> None:
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()
    with dry_run(
        {"sql": SQLiteAdapter(conn)}, audit=audit, actor="carol"
    ) as ctx:
        insert_widget(name="a")
        txn_id = ctx.txn_id
    effects = audit.get_effects(txn_id)
    assert effects[0]["actor"] == "carol"
    # Dry-run still flags itself as a dry-run alongside the actor.
    assert audit.get_transaction(txn_id)["dry_run"] == 1


# --- persisted + read back through the inspector ---------------------------


def test_actor_surfaces_in_get_timeline(conn: sqlite3.Connection) -> None:
    """The inspector's per-effect timeline view carries the actor."""
    insert_widget = _register_insert()
    # On-disk journal so the read-only JournalReader can reopen it by path.
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        audit = AuditJournal(path)
        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, audit=audit, actor="alice"
        ) as ctx:
            insert_widget(name="a")
            with acting_as("bob"):
                insert_widget(name="b")
            txn_id = ctx.txn_id
        audit.close()

        with JournalReader(path) as reader:
            timeline = reader.get_timeline(txn_id)
            assert timeline is not None
            actors = [e["actor"] for e in timeline["effects"]]
            assert actors == ["alice", "bob"]
            # stats roll-up surfaces distinct actors (the actor-axis parallel
            # to clients), NULL excluded.
            assert reader.stats()["actors"] == ["alice", "bob"]
    finally:
        os.remove(path)


# --- NULL-tolerance against a pre-actor journal ----------------------------


# The exact pre-actor ``effects`` schema (no ``actor`` column), copied from the
# commit before this field landed. A journal written by that engine must read
# back cleanly: the reader degrades the per-effect actor to ``None`` and the
# stats roll-up to empty, never KeyError-ing on the absent column.
_PRE_ACTOR_SCHEMA = """
CREATE TABLE transactions (
    txn_id        TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    replayed_from TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    client_id     TEXT
);
CREATE TABLE effects (
    txn_id     TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    effect_id  TEXT NOT NULL,
    tool       TEXT NOT NULL,
    resource   TEXT NOT NULL,
    reversible INTEGER NOT NULL,
    status     TEXT NOT NULL,
    args       TEXT NOT NULL,
    snapshot   TEXT,
    result     TEXT,
    read_keys  TEXT NOT NULL DEFAULT '[]',
    write_keys TEXT NOT NULL DEFAULT '[]',
    ts         TEXT NOT NULL,
    PRIMARY KEY (txn_id, idx)
);
"""


def _write_pre_actor_journal(path: str) -> str:
    """Hand-write a journal with the pre-actor schema; return its txn_id."""
    c = sqlite3.connect(path)
    c.executescript(_PRE_ACTOR_SCHEMA)
    c.execute(
        "INSERT INTO transactions "
        "(txn_id, state, created_at, updated_at, replayed_from, dry_run, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("txn-old", "COMMITTED", "2025-01-01T00:00:00+00:00",
         "2025-01-01T00:00:00+00:00", None, 0, "legacy-client"),
    )
    c.execute(
        "INSERT INTO effects "
        "(txn_id, idx, effect_id, tool, resource, reversible, status, args, "
        "snapshot, result, read_keys, write_keys, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("txn-old", 0, "eid0", "insert_widget", "sql", 1, "APPLIED",
         "{}", None, None, "[]", "[]", "2025-01-01T00:00:00+00:00"),
    )
    c.commit()
    c.close()
    return "txn-old"


def test_reader_tolerates_pre_actor_journal() -> None:
    """A read of a journal written before the column degrades to None — no crash.

    Failing-before guard: without ``e.get("actor")`` (a plain ``e["actor"]``)
    and the ``_has_actor`` probe, this raises KeyError / OperationalError on the
    absent column. The graceful-degrade is the thing under test.
    """
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        txn_id = _write_pre_actor_journal(path)
        with JournalReader(path) as reader:
            timeline = reader.get_timeline(txn_id)
            assert timeline is not None
            # Degrades to None rather than raising on the absent column.
            assert timeline["effects"][0]["actor"] is None
            # stats roll-up is empty (no column to read), not a crash.
            assert reader.stats()["actors"] == []
    finally:
        os.remove(path)


def test_writer_migrates_pre_actor_journal() -> None:
    """Opening a pre-actor journal with the current AuditJournal adds the column.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on the existing table, so the
    column only appears via the additive ``ALTER TABLE`` migration — old rows
    read back as NULL, and a new write can carry an actor.
    """
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        old_txn = _write_pre_actor_journal(path)
        # Reopen with the current engine: the migration runs on connect.
        audit = AuditJournal(path)
        # Old row reads back with a NULL actor (column was back-filled NULL).
        old_effects = audit.get_effects(old_txn)
        assert old_effects[0]["actor"] is None

        # A fresh write into the migrated journal carries the actor.
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")

        @tool(resource="sql", reversible=True)
        def insert_widget(c: sqlite3.Connection, name: str) -> None:
            c.execute("INSERT INTO widgets (name) VALUES (?)", (name,))

        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, audit=audit, actor="dave"
        ) as ctx:
            insert_widget(name="x")
            new_txn = ctx.txn_id
        assert audit.get_effects(new_txn)[0]["actor"] == "dave"
        audit.close()
        conn.close()
    finally:
        os.remove(path)


def test_migration_is_idempotent() -> None:
    """Re-opening an already-migrated journal does not double-add the column."""
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        AuditJournal(path).close()        # creates with the column
        AuditJournal(path).close()        # second open must not raise
        a = AuditJournal(path)
        cols = {
            r["name"]
            for r in a._conn.execute("PRAGMA table_info(effects)").fetchall()
        }
        assert "actor" in cols
        a.close()
    finally:
        os.remove(path)


# --- accessible in a policy rule -------------------------------------------


def test_policy_rule_can_branch_on_actor(conn: sqlite3.Connection) -> None:
    """A rule denies an effect based on its actor — principal-aware policy.

    Failing-before guard: a rule branching on ``effect.actor`` only has a
    principal to test because the runtime stamps it; without the threading the
    actor would be ``None`` and the deny would never fire.
    """
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()

    policy = Policy.allow_all()

    @policy.rule
    def only_alice_inserts(effect, ctx):
        # The actor is reachable directly on the effect and via the context
        # accessor — assert both surfaces agree.
        assert ctx.actor(effect) == effect.actor
        if effect.tool == "insert_widget" and effect.actor != "alice":
            return Deny(f"insert requires actor 'alice', not {effect.actor!r}")
        return Allow()

    # alice is allowed.
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, policy=policy, actor="alice"
    ):
        insert_widget(name="ok")

    # mallory is denied — the rule fires on the stamped actor.
    with pytest.raises(PolicyViolation) as exc:
        with agent_txn(
            {"sql": SQLiteAdapter(conn)},
            audit=audit,
            policy=policy,
            actor="mallory",
        ):
            insert_widget(name="nope")
    assert "mallory" in str(exc.value)


def test_policy_rule_sees_per_call_actor_override(conn: sqlite3.Connection) -> None:
    """The rule sees the ``acting_as`` override, not just the txn default."""
    insert_widget = _register_insert()
    audit = AuditJournal.in_memory()

    seen: list[str | None] = []
    policy = Policy.allow_all()

    @policy.rule
    def capture_actor(effect, ctx):
        seen.append(effect.actor)
        return Allow()

    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=audit, policy=policy, actor="default"
    ):
        insert_widget(name="a")
        with acting_as("override"):
            insert_widget(name="b")
    # The policy is evaluated twice — stage-time (per tool call) and again at
    # commit-time (the journal re-walk, TOCTOU safety). Each effect carries its
    # own stamped actor through both passes, so the rule sees the per-call
    # override at stage AND at commit: stage[a,b] then commit[a,b].
    assert seen == ["default", "override", "default", "override"]


# --- actor survives the replay fold (provenance-faithful replay) -----------


def test_replay_preserves_actor_and_is_principal_faithful(
    conn: sqlite3.Connection, tmp_path
) -> None:
    """Replaying a journal must reconstruct each effect's *original* actor.

    The actor is the on-whose-authority principal; replay is a forward fold of
    the journal against fresh state. If the fold drops ``actor`` (rebuilds each
    ``Effect`` without it), replay silently erases provenance — and a
    principal-aware policy that *would* deny on the original actor can no longer
    fire on the replayed effects.

    Failing-before guard: with the replay fold reconstructing ``Effect(...)``
    *without* ``actor=src.get("actor")``, every replayed effect's actor reads
    back as ``None``. The two assertions below — (1) the persisted replay-journal
    rows carry the same actors as the source, and (2) an actor-deny rule still
    fires when re-evaluated against the reconstructed effects — both fail until
    the actor is threaded through both reconstruction sites in ``replay.py``.
    """
    from pherix.core.effects import Effect
    from pherix.core.policy import PolicyContext
    from pherix.core.replay import replay

    insert_widget = _register_insert()

    # Source journal on disk: distinct, non-default actors per effect.
    source_audit = AuditJournal(str(tmp_path / "source.db"))
    with agent_txn(
        {"sql": SQLiteAdapter(conn)}, audit=source_audit, actor="alice"
    ) as ctx:
        insert_widget(name="a")                 # actor → "alice" (txn default)
        with acting_as("mallory"):
            insert_widget(name="b")             # actor → "mallory" (override)
        src_txn_id = ctx.txn_id

    # Sanity: the source journal recorded the two distinct actors.
    src_effects = source_audit.get_effects(src_txn_id)
    assert [e["actor"] for e in src_effects] == ["alice", "mallory"]

    # Replay forward against a fresh DB; capture the replay's own journal.
    fresh = sqlite3.connect(":memory:", isolation_level=None)
    fresh.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    target_audit = AuditJournal(str(tmp_path / "target.db"))
    result = replay(
        src_txn_id,
        {"sql": SQLiteAdapter(fresh)},
        source_audit=source_audit,
        target_audit=target_audit,
        mode="reconstruct",
    )

    # (1) The reconstructed journal carries the SAME actors as the source —
    # replay is provenance-faithful, not provenance-erasing.
    replay_effects = target_audit.get_effects(result.replay_txn_id)
    assert [e["actor"] for e in replay_effects] == ["alice", "mallory"]

    # (2) Principal-faithful: an actor-deny rule re-fires on the reconstructed
    # effects exactly as it would have on the originals. Rebuild Effect objects
    # from the replay journal (the surface a principal-aware policy sees) and
    # evaluate the rule — the deny must fire on the "mallory" effect.
    policy = Policy.allow_all()

    @policy.rule
    def only_alice_inserts(effect, ctx):
        if effect.tool == "insert_widget" and effect.actor != "alice":
            return Deny(f"insert requires actor 'alice', not {effect.actor!r}")
        return Allow()

    reconstructed = [
        Effect(
            txn_id=e["txn_id"],
            index=int(e["idx"]),
            tool=e["tool"],
            args={},
            resource=e["resource"],
            reversible=bool(e["reversible"]),
            actor=e["actor"],
        )
        for e in replay_effects
    ]
    pctx = PolicyContext(journal=reconstructed, where="commit")
    # "alice" effect passes.
    policy.evaluate(reconstructed[0], pctx)
    # "mallory" effect re-fires the deny — only possible because the actor
    # survived the replay fold.
    with pytest.raises(PolicyViolation) as exc:
        policy.evaluate(reconstructed[1], pctx)
    assert "mallory" in str(exc.value)

    source_audit.close()
    target_audit.close()
    fresh.close()
