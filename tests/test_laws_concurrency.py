"""Property-based laws of isolation under concurrent schedules.

A concurrent schedule is, for the commit-time diff, fully captured by the
version state the shared resource ends up in: which keys this transaction read
(and at what version), which it wrote (its own bumps), and which a *foreign*
transaction bumped before this one committed. We generate those schedules
randomly and assert the two invariants that matter:

- **no false conflict** — a transaction's own writes (self-bumps) never flag a
  conflict against itself; two transactions touching disjoint keys never
  conflict. This is the exact bug class a real agent's audit dogfood caught:
  a read-then-write on the same key spuriously conflicting on its own bump.
- **no lost update** — if a foreign transaction committed a write to a key this
  transaction read, the commit-time diff *always* flags it (first-committer-
  wins); the write is never silently lost.

The diff is driven deterministically (the schedule is encoded in the adapter's
version state), so these properties hold without flaky OS-thread timing — the
version state is precisely what any real interleaving would have produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pherix.core.effects import Effect
from pherix.core.isolation import Abort, IsolationConflict, check_conflicts
from pherix.core.runtime import agent_txn
from pherix.core.tools import active_effect, tool

# Trust pillar: blast radius — isolation/no-lost-update: concurrent txns do not
# corrupt one another's writes (the atomicity property under concurrency).
pytestmark = pytest.mark.blast_radius

_LAW = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

KEYS = ["x", "y", "z", "w"]


# --- a minimal own-write-visible adapter (no reads_committed_only) -----------


@dataclass
class FakeAdapter:
    """In-memory versioned adapter — versions are controllable from the test.

    No ``reads_committed_only`` method, so ``check_conflicts`` treats it as the
    own-write-visible path: ``read_version`` reflects this txn's own bumps, and
    the expected-current per key is the version after my last write.
    """

    name: str = "fake"
    versions: dict[tuple, int] = field(default_factory=dict)

    def supports_rollback(self) -> bool:
        return True

    def read_version(self, key: tuple) -> int:
        return self.versions.get(tuple(key), 0)


def _effect(read_keys, write_keys) -> Effect:
    return Effect(
        txn_id="txn-mine",
        index=0,
        tool="t",
        args={},
        resource="fake",
        reversible=True,
        read_keys=list(read_keys),
        write_keys=list(write_keys),
    )


# Per key: (read?, my_writes, foreign_bumps).
_key_schedule = st.tuples(
    st.booleans(),
    st.integers(min_value=0, max_value=3),
    st.integers(min_value=0, max_value=3),
)


@given(schedule=st.dictionaries(keys=st.sampled_from(KEYS), values=_key_schedule))
@_LAW
def test_conflict_iff_foreign_bumped_a_read_key(schedule):
    """A conflict fires on key K *iff* K was read and a foreign txn bumped it.

    Self-bumps (``my_writes``) never flag; foreign bumps on a read key always
    flag; foreign bumps on a key I only wrote (or never touched) never flag —
    Slice 4's read-write conflict scope.
    """
    read_keys = []
    write_keys = []
    versions: dict[tuple, int] = {}
    expect_conflict: set[tuple] = set()

    for key, (does_read, my_writes, foreign) in schedule.items():
        k = (key,)
        # Read captures the committed base at read time (v0 here).
        if does_read:
            read_keys.append(("fake", k, 0))
        # My own writes bump the live version monotonically; record the
        # freshest as last_my_write.
        if my_writes:
            for i in range(1, my_writes + 1):
                write_keys.append(("fake", k, i))
        # Live version now = my bumps + foreign bumps.
        versions[k] = my_writes + foreign
        # The diff only checks read keys; a foreign bump on a read key is the
        # only thing that should flag.
        if does_read and foreign > 0:
            expect_conflict.add(k)

    adapter = FakeAdapter(versions=versions)
    conflicts = check_conflicts([_effect(read_keys, write_keys)], {"fake": adapter})
    assert {c.key for c in conflicts} == expect_conflict


# --- end-to-end first-committer-wins through the runtime ---------------------


@dataclass
class MVCCFakeAdapter:
    """Reversible versioned adapter the runtime can drive end-to-end.

    Snapshots copy the touched keys; restore rewinds them. ``read``/``write``
    record into the active Effect so the commit-time diff has read/write keys.
    A single shared instance models the world two (sequenced) transactions race
    over — the proven deterministic concurrency pattern.
    """

    name: str = "fake"
    versions: dict[tuple, int] = field(default_factory=dict)
    values: dict[tuple, Any] = field(default_factory=dict)
    _snaps: dict[tuple, dict] = field(default_factory=dict)
    _active: Any = None

    def supports_rollback(self) -> bool:
        return True

    def snapshot(self, effect: Effect):
        from pherix.core.adapters.base import SnapshotHandle

        snap_key = (effect.txn_id, effect.index)
        self._snaps[snap_key] = {}
        self._active = self._snaps[snap_key]
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"snapshot_key": [effect.txn_id, effect.index]},
        )

    def apply(self, effect: Effect, tool_fn):
        return tool_fn(self, **effect.args)

    def restore(self, handle):
        sk = tuple(handle.payload["snapshot_key"])
        for k, (ver, val, had) in self._snaps.get(sk, {}).items():
            self.versions[k] = ver
            if had:
                self.values[k] = val
            else:
                self.values.pop(k, None)

    def read_version(self, key: tuple) -> int:
        return self.versions.get(tuple(key), 0)

    def write_version(self, key: tuple) -> int:
        k = tuple(key)
        self.versions[k] = self.versions.get(k, 0) + 1
        return self.versions[k]

    def read(self, key: tuple):
        eff = active_effect.get()
        if eff is not None:
            eff.read_keys.append((self.name, tuple(key), self.read_version(key)))
        return self.values.get(tuple(key))

    def write(self, key: tuple, value: Any):
        k = tuple(key)
        if self._active is not None and k not in self._active:
            self._active[k] = (self.versions.get(k, 0), self.values.get(k), k in self.values)
        self.values[k] = value
        v = self.write_version(k)
        eff = active_effect.get()
        if eff is not None:
            eff.write_keys.append((self.name, k, v))
        return v


@pytest.fixture
def mvcc_tools():
    @tool(resource="fake", injects_handle=True)
    def read_k(adapter, key):
        return adapter.read(key)

    @tool(resource="fake", injects_handle=True)
    def write_k(adapter, key, value):
        adapter.write(key, value)
        return value

    return read_k, write_k


@given(
    a_reads=st.sets(st.sampled_from(KEYS), min_size=1),
    b_writes=st.sets(st.sampled_from(KEYS)),
)
@_LAW
def test_read_only_loser_conflicts_iff_overlap(mvcc_tools, a_reads, b_writes):
    """A reads a set of keys; B (committing first) writes a set of keys; A then
    commits read-only. A conflicts under Abort *iff* B wrote a key A read — no
    lost update on overlap, no false conflict on disjoint sets."""
    read_k, write_k = mvcc_tools
    shared = MVCCFakeAdapter()
    overlap = a_reads & b_writes

    def body():
        with agent_txn({"fake": shared}, isolation=Abort()) as ctx_a:
            for key in sorted(a_reads):
                read_k(key=(key,))
            # B races in and commits first.
            with agent_txn({"fake": shared}) as ctx_b:
                for key in sorted(b_writes):
                    write_k(key=(key,), value="from-B")
            # A is read-only; its commit diff now sees B's bumps on overlap.

    if overlap:
        with pytest.raises(IsolationConflict) as info:
            body()
        assert {c.key for c in info.value.conflicts} == {(k,) for k in overlap}
    else:
        body()  # disjoint: both commit cleanly, no false conflict
        # B's writes are the canonical committed state.
        for key in b_writes:
            assert shared.values[(key,)] == "from-B"
