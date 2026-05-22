"""Cross-adapter conformance battery — ONE parametrized suite, every backend.

This is the highest-leverage test in the suite: the adapter *laws* expressed
once and folded over every adapter in the catalog (SQLite, Memory, Filesystem,
Redis, S3, MongoDB, Postgres — reversible — plus HTTP — irreversible). The
per-backend wiring (how each is stood up offline, how its touched resource is
read back) lives in ``tests/_conformance.py``; this module is pure law.

Why this shape earns its place over the per-adapter ``test_adapters_*.py``
files: those prove each backend in isolation. This suite proves they all obey
the *same* contract — so a regression in any one backend, or a tenth adapter
added without honouring the protocol, fails here against the identical
assertion. Adding a backend is one registry line in ``_conformance.py``; the
laws below then apply to it automatically.

Laws asserted:

1. **Round-trip identity** — ``snapshot → mutate → restore`` returns the
   touched resource to byte-identical state, across insert / overwrite /
   delete / delete-absent. The core "rollback ≈ identity" law per backend,
   folded through the *production* snapshot/restore code (not a toy).
2. **Version semantics** — ``read_version`` of an absent key returns the
   family's non-None sentinel; a write bumps (counter) or recomputes (hash);
   the commit-time conflict diff flags a read whose key was changed by another
   writer. Gated on adapters that implement versioning, asserting the right
   contract per family.
3. **Isolation recording** — driving a reversible adapter through the runtime
   records read/write keys into the journal.
4. **Irreversibility** (HTTP) — ``supports_rollback() is False``,
   snapshot/restore refuse, and an effect routes down the staging + gate lane.

Backends with no in-process fake (Postgres) skip cleanly when no server is
reachable, exactly as ``tests/test_adapters_postgres.py`` does — never a
silent pass.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests import _conformance as conf
from pherix.core.adapters.base import VersionedResourceAdapter
from pherix.core.effects import Effect, EffectStatus
from pherix.core.runtime import agent_txn


# ===========================================================================
# Helpers shared by the laws below.
# ===========================================================================


def _make_effect(resource: str, args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="conf",
        index=index,
        tool="conf_tool",
        args=args,
        resource=resource,
        reversible=True,
    )


def _bind_pg_tool(case: conf.AdapterCase, world, mutation):
    """Postgres and MySQL' ``apply`` injects only the bare connection, so the
    scratch-table name cannot ride through ``effect.args``. For those cases we
    wrap the world-aware tool in a closure that re-packs ``(conn, table)`` from
    the world the law already holds. Every other backend's ``apply`` passes
    the handle the dump/tools expect, so this wrapping is a no-op there.
    """
    inner = case.tool_for(mutation)
    if case.name in ("postgres", "mysql"):
        _conn, table = world

        def wrapped(conn, key, value):
            return inner((conn, table), key=key, value=value)

        return wrapped
    return inner


def _seed(case: conf.AdapterCase, world, key, value):
    case.seed(world, key, value)


def _dump(case: conf.AdapterCase, world):
    return case.dump(world)


# ===========================================================================
# Law 1 — round-trip identity: snapshot → mutate → restore ≈ identity.
# ===========================================================================


@pytest.mark.parametrize(
    "case", conf.REVERSIBLE_CASES, ids=[c.name for c in conf.REVERSIBLE_CASES]
)
@pytest.mark.parametrize("mutation", conf.ALL_MUTATIONS)
def test_round_trip_identity(case: conf.AdapterCase, mutation: str):
    """For every reversible adapter and every mutation shape, the resource is
    byte-identical after restore to what it was before the effect.

    The key starts seeded (so OVERWRITE / DELETE have something to act on) for
    every mutation except INSERT and DELETE_ABSENT, which act on a key that does
    not exist. The pre-image is captured as the comparable dump; restore must
    land exactly there regardless of what apply did.
    """
    key = "conf_key"
    with case.factory() as (adapter, world):
        # Seed for the mutations that need a pre-existing key.
        if mutation in (conf.OVERWRITE, conf.DELETE):
            _seed(case, world, key, "before")

        before = _dump(case, world)

        effect = _make_effect(adapter.name, case.args_for(mutation, key, "after"))
        handle = adapter.snapshot(effect)
        effect.snapshot = handle

        tool = _bind_pg_tool(case, world, mutation)
        # DELETE_ABSENT on a backend that errors when deleting a missing key
        # (filesystem unlink) must still leave a restorable snapshot — the
        # snapshot is taken BEFORE apply, so even an apply that raises is fine.
        try:
            adapter.apply(effect, tool)
        except Exception:
            # The mutation itself may legitimately raise (delete-absent on FS);
            # the law is about restore, which the pre-apply snapshot guarantees.
            pass

        adapter.restore(handle)
        after = _dump(case, world)

    assert after == before, (
        f"{case.name}/{mutation}: restore did not return to the pre-effect "
        f"state. before={before!r} after={after!r}"
    )


@pytest.mark.parametrize(
    "case", conf.REVERSIBLE_CASES, ids=[c.name for c in conf.REVERSIBLE_CASES]
)
def test_round_trip_overwrite_changes_value_then_restores(case: conf.AdapterCase):
    """A sharper overwrite law: the mutation must genuinely change the value
    (so the test cannot pass vacuously), and restore must bring the OLD value
    back — not merely leave the key present.
    """
    key = "conf_key"
    with case.factory() as (adapter, world):
        _seed(case, world, key, "ORIGINAL")
        before = _dump(case, world)

        effect = _make_effect(adapter.name, case.args_for(conf.OVERWRITE, key, "CHANGED"))
        handle = adapter.snapshot(effect)
        effect.snapshot = handle
        tool = _bind_pg_tool(case, world, conf.OVERWRITE)
        adapter.apply(effect, tool)

        mutated = _dump(case, world)
        assert mutated != before, (
            f"{case.name}: overwrite did not change the resource — the round-trip "
            f"law would pass vacuously"
        )

        adapter.restore(handle)
        assert _dump(case, world) == before


@pytest.mark.parametrize(
    "case", conf.REVERSIBLE_CASES, ids=[c.name for c in conf.REVERSIBLE_CASES]
)
def test_multi_effect_backward_fold_restores_to_origin(case: conf.AdapterCase):
    """Newest-first backward fold over several effects lands at the origin.

    This is the journal's rollback fold in miniature: apply N effects, then
    restore their snapshots in reverse, asserting the resource returns to its
    pre-fold state. Catches a backend whose restore is not composable across
    effects (e.g. a savepoint name collision, or a backup dir overwrite).
    """
    with case.factory() as (adapter, world):
        _seed(case, world, "k0", "v0")
        origin = _dump(case, world)

        handles = []
        for i, (k, v) in enumerate([("k1", "a"), ("k0", "b"), ("k2", "c")]):
            effect = _make_effect(
                adapter.name, case.args_for(conf.OVERWRITE, k, v), index=i
            )
            h = adapter.snapshot(effect)
            effect.snapshot = h
            handles.append(h)
            adapter.apply(effect, _bind_pg_tool(case, world, conf.OVERWRITE))

        # Backward fold: newest snapshot first.
        for h in reversed(handles):
            adapter.restore(h)

        assert _dump(case, world) == origin


# ===========================================================================
# Law 2 — version semantics (Slice 4 isolation substrate).
# ===========================================================================


@pytest.mark.parametrize(
    "vcase", conf.VERSION_CASES, ids=[c.name for c in conf.VERSION_CASES]
)
def test_read_version_absent_returns_non_none_sentinel(vcase: conf.VersionCase):
    """An absent key reads as the family's non-None sentinel — never ``None``.

    The non-None contract is load-bearing: it lets "I read this key as absent,
    then someone created it" flag as a conflict via plain ``!=`` at commit. A
    regression returning ``None`` would silently break that.
    """
    with vcase.factory() as (adapter, world):
        key = vcase.make_key()
        v = adapter.read_version(key)
        assert v is not None
        assert v == vcase.missing


@pytest.mark.parametrize(
    "vcase", conf.VERSION_CASES, ids=[c.name for c in conf.VERSION_CASES]
)
def test_write_bumps_version_away_from_sentinel(vcase: conf.VersionCase):
    """After a write, the version differs from the absent sentinel.

    For counters: monotonic increase off 0. For content hashes: a concrete
    sha256 distinct from ``"__missing__"``. Either way, "written" must be
    distinguishable from "absent".
    """
    with vcase.factory() as (adapter, world):
        key = vcase.make_key()
        assert adapter.read_version(key) == vcase.missing
        vcase.write(adapter, world, key, "payload-1")
        bumped = adapter.read_version(key)
        assert bumped != vcase.missing

        if vcase.family == "counter":
            assert isinstance(bumped, int) and bumped >= 1


@pytest.mark.parametrize(
    "vcase",
    [c for c in conf.VERSION_CASES if c.family == "counter"],
    ids=[c.name for c in conf.VERSION_CASES if c.family == "counter"],
)
def test_counter_version_is_monotonic(vcase: conf.VersionCase):
    """SQL-family counters bump monotonically and per-key independently."""
    with vcase.factory() as (adapter, world):
        k1 = vcase.make_key()
        k2 = vcase.make_key()
        assert adapter.write_version(k1) == 1
        assert adapter.write_version(k1) == 2
        assert adapter.write_version(k2) == 1
        assert adapter.read_version(k1) == 2
        assert adapter.read_version(k2) == 1


@pytest.mark.parametrize(
    "vcase",
    [c for c in conf.VERSION_CASES if c.family == "hash"],
    ids=[c.name for c in conf.VERSION_CASES if c.family == "hash"],
)
def test_hash_version_recomputes_on_content_change(vcase: conf.VersionCase):
    """Content-addressed versions track the content: same bytes → same hash,
    different bytes → different hash. This is what makes "someone rewrote what
    I read" detectable without a counter side-table.
    """
    with vcase.factory() as (adapter, world):
        key = vcase.make_key()
        vcase.write(adapter, world, key, "alpha")
        h1 = adapter.read_version(key)
        vcase.write(adapter, world, key, "alpha")
        h2 = adapter.read_version(key)
        vcase.write(adapter, world, key, "beta")
        h3 = adapter.read_version(key)

        assert h1 == h2, "identical content must hash identically"
        assert h3 != h1, "changed content must change the version hash"


@pytest.mark.parametrize(
    "vcase", conf.VERSION_CASES, ids=[c.name for c in conf.VERSION_CASES]
)
def test_versioned_adapters_declare_the_protocol_methods(vcase: conf.VersionCase):
    """Every versioned adapter exposes both halves of the version contract.

    ``VersionedResourceAdapter`` is intentionally not ``@runtime_checkable``
    (method presence is not behavioural conformance), so we assert the methods
    exist directly — the honest gate the runtime uses is
    ``supports_rollback()``, which these all satisfy.
    """
    with vcase.factory() as (adapter, world):
        assert hasattr(adapter, "read_version")
        assert hasattr(adapter, "write_version")
        assert adapter.supports_rollback() is True


# ===========================================================================
# Law 3 — isolation read/write-key recording through the runtime.
#
# Driven through the *runtime* (not the adapter directly) so the active_effect
# contextvar is bound and the handles record into the journal — the path Slice
# 4's commit-time diff folds over. We use SQLite + Memory + Filesystem, the
# three reversible adapters whose tools record keys via the handle/helper. (The
# route-b adapters record write_keys off args at a different layer and are
# covered by their own per-adapter suites; this law is about the contextvar
# recording path being live end-to-end.)
# ===========================================================================


def test_isolation_recording_sqlite_through_runtime():
    """A SQL write through the runtime records a write_key on the effect."""
    from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    adapter = SQLiteAdapter(conn)

    from pherix.core.tools import tool

    @tool(resource="sql")
    def write_row(c, name):
        execute_isolated(
            c,
            "INSERT INTO t (id, name) VALUES (1, ?)",
            (name,),
            writes=[("t", 1)],
        )

    with agent_txn({"sql": adapter}) as ctx:
        write_row(name="alice")

    effects = ctx.txn.effects
    assert len(effects) == 1
    wkeys = effects[0].write_keys
    assert any(entry[0] == "sql" and entry[1] == ("t", 1) for entry in wkeys)
    conn.close()


def test_isolation_recording_filesystem_through_runtime():
    """A filesystem read + write through the runtime records both keys."""
    import tempfile

    from pherix.core.adapters.filesystem import FilesystemAdapter
    from pherix.core.tools import tool

    with tempfile.TemporaryDirectory() as root:
        adapter = FilesystemAdapter(root)

        @tool(resource="fs")
        def touch(handle, path, data):
            handle.write(path, data.encode())
            handle.read(path)

        with agent_txn({"fs": adapter}) as ctx:
            touch(path="note.txt", data="hello")

        effect = ctx.txn.effects[0]
        write_paths = {entry[1] for entry in effect.write_keys}
        read_paths = {entry[1] for entry in effect.read_keys}
        assert ("note.txt",) in write_paths
        assert ("note.txt",) in read_paths


def test_isolation_recording_memory_through_runtime():
    """A memory remember + recall through the runtime records both keys."""
    from pherix.core.adapters.memory import MemoryAdapter
    from pherix.core.tools import tool

    conn = sqlite3.connect(":memory:", isolation_level=None)
    adapter = MemoryAdapter(conn, namespace="conf")

    @tool(resource="memory")
    def remember_then_recall(handle, key, value):
        handle.remember(key, value)
        handle.recall(key)

    with agent_txn({"memory": adapter}) as ctx:
        remember_then_recall(key="fact", value="42")

    effect = ctx.txn.effects[0]
    write_keys = {entry[1] for entry in effect.write_keys}
    read_keys = {entry[1] for entry in effect.read_keys}
    assert ("fact",) in write_keys
    assert ("fact",) in read_keys
    conn.close()


def test_commit_time_diff_flags_a_concurrently_changed_read_key():
    """The version contract's payoff: a key read at version V, changed by
    another writer, re-read at commit as V', flags a conflict.

    Built on the SQLite counter family. Txn A reads key (t,1) at its current
    version, then a separate writer bumps that key's version; A's commit-time
    diff must detect the mismatch. This is the regression class the whole
    version contract exists to catch — if read_version stopped reflecting
    cross-txn bumps, this silently passes a lost update.
    """
    from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
    from pherix.core.isolation import check_conflicts

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    adapter = SQLiteAdapter(conn)

    # Build an effect that RECORDS a read of (t,1) at the current version, by
    # hand (mirrors what a tool's execute_isolated read does).
    adapter.begin()
    effect = _make_effect("sql", {})
    v_at_read = adapter.read_version(("t", 1))
    effect.read_keys.append(("sql", ("t", 1), v_at_read))

    # A concurrent writer bumps the version of that key.
    adapter.write_version(("t", 1))

    conflicts = check_conflicts([effect], {"sql": adapter})
    assert conflicts, (
        "commit-time diff failed to flag a read key whose version moved — the "
        "version contract's core lost-update protection regressed"
    )
    adapter.rollback()
    conn.close()


# ===========================================================================
# Law 4 — irreversibility (HTTP): honest "I cannot undo".
# ===========================================================================


@pytest.mark.parametrize(
    "case", conf.IRREVERSIBLE_CASES, ids=[c.name for c in conf.IRREVERSIBLE_CASES]
)
def test_irreversible_reports_no_rollback(case: conf.AdapterCase):
    with case.factory() as (adapter, _world):
        assert adapter.supports_rollback() is False


@pytest.mark.parametrize(
    "case", conf.IRREVERSIBLE_CASES, ids=[c.name for c in conf.IRREVERSIBLE_CASES]
)
def test_irreversible_refuses_snapshot_and_restore(case: conf.AdapterCase):
    from pherix.core.adapters.http import IrreversibleAdapterError
    from pherix.core.adapters.base import SnapshotHandle

    with case.factory() as (adapter, _world):
        effect = _make_effect(adapter.name, {"url": "x", "body": "y"})
        with pytest.raises(IrreversibleAdapterError):
            adapter.snapshot(effect)
        with pytest.raises(IrreversibleAdapterError):
            adapter.restore(SnapshotHandle(resource=adapter.name, effect_index=0, payload={}))


def test_irreversible_effect_routes_down_the_staging_lane():
    """Through the runtime, an HTTP tool stages (returns a StagedResult, no
    live fire) and fires only at commit — the irreversible lane's defining
    property. Asserts the agent gets a sentinel pre-commit and the real call
    fires exactly once at commit.
    """
    from pherix.core.adapters.http import HTTPAdapter
    from pherix.core.effects import StagedResult
    from pherix.core.tools import tool

    fired = []

    @tool(resource="http", reversible=False, injects_handle=False, compensator="undo")
    def post_webhook(url, body):
        fired.append((url, body))
        return {"status": 200}

    @tool(resource="http", reversible=False, injects_handle=False)
    def undo(url, body):
        pass

    with agent_txn({"http": HTTPAdapter()}) as ctx:
        result = post_webhook(url="https://x", body="ping")
        # Pre-commit: staged, not fired.
        assert isinstance(result, StagedResult)
        assert fired == []

    # Post-commit (clean exit auto-commits): fired exactly once.
    assert fired == [("https://x", "ping")]
    assert ctx.txn.effects[0].status is EffectStatus.APPLIED


def test_irreversible_without_compensator_gates_at_commit():
    """A staged irreversible with no compensator and no approval blocks the
    commit at the gate, unwinds, and never fires. This is the honest-undo
    stance: the gate is the undo.
    """
    from pherix.core.adapters.http import HTTPAdapter
    from pherix.core.runtime import GateBlocked
    from pherix.core.tools import tool

    fired = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, subject, body):
        fired.append(to)

    with pytest.raises(GateBlocked):
        with agent_txn({"http": HTTPAdapter()}):
            send_email(to="a@b.c", subject="hi", body="x")

    # Gate blocked → the effect never fired.
    assert fired == []


def test_irreversible_with_approval_fires_at_commit():
    """The other side of the gate: an explicit ``approve_irreversible`` lets a
    compensator-less irreversible through, and it fires once at commit.
    """
    from pherix.core.adapters.http import HTTPAdapter
    from pherix.core.effects import StagedResult
    from pherix.core.tools import tool

    fired = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def send_email(to, subject, body):
        fired.append(to)

    with agent_txn({"http": HTTPAdapter()}) as ctx:
        staged = send_email(to="a@b.c", subject="hi", body="x")
        assert isinstance(staged, StagedResult)
        ctx.approve_irreversible(staged.effect_id)

    assert fired == ["a@b.c"]
