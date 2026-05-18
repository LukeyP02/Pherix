"""Slice 4 — resolution policies end-to-end through agent_txn / run_txn.

The canonical pin is the lost-update scenario (TASK.md):

    Txn A opens, reads X (records read_keys at v5).
    Txn B writes X (bumps to v6) and commits.
    Txn A commits → resolution policy fires.

Each policy produces a different, serializability-preserving outcome:
    * Abort: IsolationConflict raised, A rolls back; world == post-B.
    * Retry(N): A re-runs against post-B state, succeeds (if not exhausted).
    * Serialize: A's commit waits for B to close — but in the single-
      threaded test we drive B to completion first, so Serialize falls
      through to the diff and degrades to Abort. The cross-thread Serialize
      story is pinned by ``test_serialize_waits_for_concurrent_writer``.

Stream A's real adapters might still be in flight; tests use a hand-
written :class:`FakeAdapter` so they pin only Stream C's contract.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect
from pherix.core.isolation import (
    REGISTRY as ISOLATION_REGISTRY,
    Abort,
    IsolationConflict,
    Retry,
    Serialize,
)
from pherix.core.runtime import agent_txn
from pherix.core.tools import active_effect, tool
from pherix.frontends.library import run_txn


# --- fake adapter ------------------------------------------------------------


@dataclass
class FakeAdapter:
    """In-memory adapter with controllable per-key versions.

    Conforms to :class:`ResourceAdapter` shape so the runtime can drive
    snapshot/apply/restore — the snapshot is just a copy of the key->value
    dict; restore swaps it back. Reads record into ``active_effect`` so the
    journal carries the version-at-read for the commit-time diff.
    """

    name: str = "fake"
    versions: dict[tuple, int] = field(default_factory=dict)
    values: dict[tuple, Any] = field(default_factory=dict)
    # (txn_id, effect_index) -> {key: (pre_version, pre_value, had_value)}.
    _snapshots: dict[tuple, dict] = field(default_factory=dict)
    _active_snapshot: Any = None  # the current effect's snapshot dict

    def supports_rollback(self) -> bool:
        return True

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        # Per-effect: record which keys this effect touches so restore can
        # reset only THOSE keys. Snapshot index is keyed by (txn_id,
        # effect.index) so concurrent transactions on the same adapter do
        # not collide. The audit journal carries the index pair as a list
        # in the payload so it stays JSON-serialisable.
        snap_key = (effect.txn_id, effect.index)
        self._snapshots[snap_key] = {}
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"snapshot_key": [effect.txn_id, effect.index]},
        )

    def apply(self, effect: Effect, tool_fn) -> Any:
        # The tool runs raw — it interacts with the adapter via the
        # ``injects_handle=True`` form. The injected handle is ``self``
        # so the tool can call read/write on us. We bind the current
        # effect's snapshot dict so ``write`` can stash the pre-state of
        # any key it touches first-touch-style.
        snap_key = (effect.txn_id, effect.index)
        self._active_snapshot = self._snapshots.get(snap_key)
        try:
            return tool_fn(self, **effect.args)
        finally:
            self._active_snapshot = None

    def restore(self, handle: SnapshotHandle) -> None:
        # Per-key restore: only the keys this effect touched get rewound.
        # Other keys (including ones a concurrent txn committed during
        # our lifetime) are left untouched — matching the real adapters.
        snap_key = tuple(handle.payload["snapshot_key"])
        snap = self._snapshots.pop(snap_key)
        for key, (pre_version, pre_value, had_value) in snap.items():
            self.versions[key] = pre_version
            if had_value:
                self.values[key] = pre_value
            else:
                self.values.pop(key, None)

    def read_version(self, key: tuple) -> int:
        return self.versions.get(tuple(key), 0)

    def write_version(self, key: tuple) -> int:
        v = self.versions.get(tuple(key), 0) + 1
        self.versions[tuple(key)] = v
        return v

    # --- direct accessors used by tools below -------------------------------

    def read(self, key: tuple) -> Any:
        # Tools call this; we record the (resource, key, version) tuple
        # into the active Effect so the commit-time diff has something to
        # work with.
        eff = active_effect.get()
        if eff is not None:
            eff.read_keys.append((self.name, tuple(key), self.read_version(key)))
        return self.values.get(tuple(key))

    def write(self, key: tuple, value: Any) -> None:
        k = tuple(key)
        # First-touch capture (matches the FsHandle / SQL savepoint
        # discipline): record this key's pre-state into the active
        # snapshot so restore can rewind exactly this key.
        if self._active_snapshot is not None and k not in self._active_snapshot:
            had = k in self.values
            self._active_snapshot[k] = (
                self.versions.get(k, 0),
                self.values.get(k),
                had,
            )
        self.values[k] = value
        v = self.write_version(k)
        eff = active_effect.get()
        if eff is not None:
            eff.write_keys.append((self.name, k))
        return v


# --- the canonical lost-update scenario -------------------------------------


def test_lost_update_under_abort_raises_isolation_conflict():
    """Txn A reads X@v0; Txn B writes X (→v1) and commits; Txn A commits.

    Under :class:`Abort` the diff fires :class:`IsolationConflict` and
    Txn A rolls back. Txn A is read-only — :func:`check_conflicts`
    filters self-bumps under Slice 4's monotonic-counter versioning, so
    the canonical pin uses a read-only "loser" txn for which
    first-committer-wins is unambiguous.
    """
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    with pytest.raises(IsolationConflict) as info:
        with agent_txn({"fake": shared}, isolation=Abort()) as ctx_a:
            read_x(key=("x",))
            # Concurrently: Txn B does its thing.
            with agent_txn({"fake": shared}) as ctx_b:
                write_x(key=("x",), value="from-B")
            # B has committed; the shared adapter's version of ("x",) is
            # now 1. A is read-only — its commit diff will see v0 != v1.

    # The conflict points at the read A made before B committed.
    assert len(info.value.conflicts) == 1
    c = info.value.conflicts[0]
    assert c.resource == "fake"
    assert c.key == ("x",)
    assert c.version_at_read == 0
    assert c.version_now == 1


def test_lost_update_under_retry_replays_and_succeeds():
    """run_txn(fn, isolation=Retry(2)) replays fn after a conflict.

    First attempt: A reads X at v0, B commits a write (v→1), A's commit
    diff fires (read-only A, so the diff is clean of self-bumps) →
    rollback + retry. Second attempt: A reads X at v1, world quiet →
    commits. ``attempts`` proves the re-entry actually happened.
    """
    shared = FakeAdapter()
    attempts = {"n": 0}

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    # One-shot side-effect: B commits a write on the FIRST entry into fn,
    # then never again — the second attempt sees the post-B world quiet.
    b_committed = {"done": False}

    def fn(ctx):
        attempts["n"] += 1
        read_x(key=("x",))
        if not b_committed["done"]:
            with agent_txn({"fake": shared}) as ctx_b:
                write_x(key=("x",), value="from-B")
            b_committed["done"] = True
        # A is read-only — the canonical Slice 4 lost-update shape.

    run_txn(fn, {"fake": shared}, isolation=Retry(max_attempts=3))

    # The second attempt succeeded; B's value remains canonical (A wrote
    # nothing). Two attempts total — first failed, second succeeded.
    assert shared.values[("x",)] == "from-B"
    assert attempts["n"] == 2


def test_retry_exhaustion_raises_isolation_conflict():
    """Retry(1) with a fn that always conflicts → raises on the single attempt."""
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    def fn(ctx):
        read_x(key=("x",))
        # Every entry: another writer commits a bump → guaranteed conflict.
        # A is read-only so the diff fires cleanly under monotonic counters.
        with agent_txn({"fake": shared}) as ctx_b:
            write_x(key=("x",), value="bump")

    with pytest.raises(IsolationConflict):
        run_txn(fn, {"fake": shared}, isolation=Retry(max_attempts=1))


def test_retry_exhaustion_after_max_attempts():
    """Retry(2): two attempts, both conflict → IsolationConflict raised."""
    shared = FakeAdapter()
    attempts = {"n": 0}

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    def fn(ctx):
        attempts["n"] += 1
        read_x(key=("x",))
        with agent_txn({"fake": shared}) as ctx_b:
            write_x(key=("x",), value="bump")
        # A read-only — diff sees v_at_read != v_now (bumped by B).

    with pytest.raises(IsolationConflict):
        run_txn(fn, {"fake": shared}, isolation=Retry(max_attempts=2))
    assert attempts["n"] == 2


def test_retry_in_with_form_degrades_to_abort():
    """``with agent_txn(..., isolation=Retry(N))`` cannot re-enter from
    outside Pherix. The contract: Retry degrades to Abort cleanly — the
    caller sees the public :class:`IsolationConflict`, never the internal
    :class:`_RetrySignal`. :data:`_in_run_txn` (default False) is what
    selects between the two paths in :meth:`Retry.resolve`.
    """
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    with pytest.raises(IsolationConflict) as info:
        with agent_txn({"fake": shared}, isolation=Retry(max_attempts=3)):
            read_x(key=("x",))
            with agent_txn({"fake": shared}) as ctx_b:
                write_x(key=("x",), value="bump")
            # A is read-only — diff fires; Retry.resolve sees
            # _in_run_txn == False and raises IsolationConflict, not the
            # private _RetrySignal name.

    # The conflict carries the moved key, same shape Abort would produce.
    assert len(info.value.conflicts) == 1
    assert info.value.conflicts[0].key == ("x",)
    # The internal signal type must NOT leak: confirm the raised
    # exception is the public type, not the private one.
    from pherix.core.isolation import _RetrySignal
    assert not isinstance(info.value, _RetrySignal)


# --- Serialize ---------------------------------------------------------------


def test_serialize_with_quiet_world_proceeds_cleanly():
    """No concurrent in-flight txn touches my reads → Serialize commits."""
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    with agent_txn({"fake": shared}, isolation=Serialize()) as ctx:
        read_x(key=("x",))
        write_x(key=("x",), value="solo")

    assert shared.values[("x",)] == "solo"


def test_serialize_with_already_committed_writer_falls_through_to_diff():
    """Other writer already committed (no longer in-flight) so Serialize
    has nobody to wait on. The diff then fires under Serialize, which
    degrades to Abort behaviour. A is read-only so the canonical
    monotonic-counter diff fires cleanly.
    """
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    with pytest.raises(IsolationConflict):
        with agent_txn(
            {"fake": shared}, isolation=Serialize(timeout_seconds=0.1)
        ) as ctx_a:
            read_x(key=("x",))
            # B opens AND closes — by the time A reaches commit, B is no
            # longer in-flight, so Serialize has nobody to wait on.
            with agent_txn({"fake": shared}) as ctx_b:
                write_x(key=("x",), value="from-B")


def test_serialize_waits_for_concurrent_in_flight_writer():
    """Cross-thread: A reads X at v0, then B opens in another thread,
    queues a write on X, and holds the txn open. A's Serialize commit
    waits on B's close before running the diff. Once B commits and the
    version bumps, A re-checks — and since A's read was at v0 with B
    having pushed it to v1, A flags an IsolationConflict.

    The pin is on the *waiting*, not just the conflict outcome. Verified
    by total elapsed time: B sleeps 50ms before committing; A's commit
    must finish AFTER B does (i.e. it actually waited).
    """
    import time

    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    # Step 1: A reads X first while no one else is around — captures v0.
    # We can't keep A open across the thread boundary cleanly (contextvars
    # don't cross threads), so the sequencing here is: open A, do the
    # read, leave A's commit pending in the main thread; then start B in
    # another thread; then resume A's commit so it hits the wait path.

    a_ready_to_commit = threading.Event()
    b_started = threading.Event()
    b_finished_at: dict[str, float] = {}

    def run_b():
        # Wait until A has done its read and is paused on the brink of
        # committing — so the write happens *after* A's read recorded v0.
        a_ready_to_commit.wait(timeout=1.0)
        with agent_txn({"fake": shared}) as ctx_b:
            write_x(key=("x",), value="from-B")
            b_started.set()
            time.sleep(0.05)  # hold the txn open while A's commit waits
        b_finished_at["t"] = time.monotonic()

    thread = threading.Thread(target=run_b)
    thread.start()

    a_finished_at: dict[str, float] = {}
    with pytest.raises(IsolationConflict):
        with agent_txn(
            {"fake": shared}, isolation=Serialize(timeout_seconds=2.0)
        ) as ctx_a:
            read_x(key=("x",))
            # A has recorded read_keys at v0. Now let B run and queue its
            # write — A's commit (on __exit__ below) will then wait on B.
            a_ready_to_commit.set()
            b_started.wait(timeout=1.0)
            # Commit fires here; Serialize finds B in-flight with a write
            # intersecting A's reads → blocks until B closes → diff fires
            # → IsolationConflict.

    a_finished_at["t"] = time.monotonic()
    thread.join()

    # A's commit must have waited for B to finish.
    assert a_finished_at["t"] >= b_finished_at["t"]


# --- in-process shared journal (D5) -----------------------------------------


def test_two_in_process_txns_register_with_the_journal_registry():
    """Both ctxs visible to the in-process arbitration substrate while open."""
    shared = FakeAdapter()
    seen: list[set[str]] = []

    @tool(resource="fake", injects_handle=True)
    def noop(adapter):
        return None

    with agent_txn({"fake": shared}) as ctx_a:
        with agent_txn({"fake": shared}) as ctx_b:
            ids = {c.txn_id for c in ISOLATION_REGISTRY.open_contexts()}
            seen.append(ids)
            noop()  # silence unused warnings

    assert seen[0].issuperset({ctx_a.txn_id, ctx_b.txn_id})
    # After both close, neither is open.
    open_ids = {c.txn_id for c in ISOLATION_REGISTRY.open_contexts()}
    assert ctx_a.txn_id not in open_ids
    assert ctx_b.txn_id not in open_ids


def test_in_process_conflict_fires_through_shared_state():
    """Two TxnContexts in one process; A reads X then B commits a write
    on X; A's commit must flag the conflict via the shared adapter
    state — the JournalRegistry is the arbiter."""
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def read_x(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_x(adapter, key, value):
        adapter.write(key, value)
        return value

    with pytest.raises(IsolationConflict):
        with agent_txn({"fake": shared}, isolation=Abort()) as ctx_a:
            read_x(key=("shared-row",))
            # Second txn opens, writes, commits — A's read version is now stale.
            with agent_txn({"fake": shared}) as ctx_b:
                write_x(key=("shared-row",), value=42)


# --- backward-compatibility: agent_txn signature -----------------------------


def test_default_isolation_is_abort():
    """Existing tests not passing isolation= must still work: default is
    :class:`Abort`. A no-conflict commit succeeds; no behaviour change."""
    shared = FakeAdapter()

    @tool(resource="fake", injects_handle=True)
    def noop(adapter):
        return None

    with agent_txn({"fake": shared}) as ctx:
        noop()
    assert ctx.txn.state.name == "COMMITTED"


def test_run_txn_with_abort_is_equivalent_to_with_form():
    """``run_txn(fn, ..., isolation=Abort())`` should behave exactly like
    ``with agent_txn(...) as ctx: fn(ctx)`` in the no-conflict case."""
    shared = FakeAdapter()
    visited = {"n": 0}

    @tool(resource="fake", injects_handle=True)
    def noop(adapter):
        return None

    def fn(ctx):
        visited["n"] += 1
        noop()

    run_txn(fn, {"fake": shared}, isolation=Abort())
    assert visited["n"] == 1
