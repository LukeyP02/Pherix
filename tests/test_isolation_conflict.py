"""Slice 4 — the commit-time conflict diff (Stream C).

The diff itself is pure: given a list of effects and a dict of adapters,
walk every read_key and ask the adapter for the current version. The
algorithmic heart is small but the contract has sharp edges — non-rollback
adapters must be skipped, missing adapters must be skipped, the read_key
shape is fixed, and the Conflict carries everything an operator needs to
diagnose what moved.

These tests use a hand-written fake adapter rather than the real
:class:`SQLiteAdapter` so they pin only the diff's behaviour. The real
adapter behaviour is pinned in :mod:`tests.test_isolation_versions`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from pherix.core.effects import Effect
from pherix.core.isolation import (
    Abort,
    Conflict,
    IsolationConflict,
    Retry,
    Serialize,
    _RetrySignal,
    check_conflicts,
)


# --- fake adapters -----------------------------------------------------------


@dataclass
class FakeAdapter:
    """In-memory adapter whose versions are controllable from the test.

    Conforms to the bits of the ResourceAdapter contract :func:`check_conflicts`
    actually touches: ``supports_rollback()`` and ``read_version(key)``. The
    snapshot/apply/restore methods exist only so a structural ``isinstance``
    check against the protocol would succeed; the diff never calls them.
    """

    name: str = "fake"
    rollback_ok: bool = True
    versions: dict[tuple, Any] = field(default_factory=dict)

    def supports_rollback(self) -> bool:
        return self.rollback_ok

    def read_version(self, key: tuple) -> Any:
        return self.versions.get(tuple(key), 0)

    # The methods below are never invoked by the diff but exist for shape.
    def snapshot(self, effect):  # pragma: no cover
        raise NotImplementedError

    def apply(self, effect, tool_fn):  # pragma: no cover
        raise NotImplementedError

    def restore(self, handle):  # pragma: no cover
        raise NotImplementedError


def _effect(
    *,
    index: int = 0,
    resource: str = "fake",
    reversible: bool = True,
    read_keys: list[tuple] | None = None,
    write_keys: list[tuple] | None = None,
) -> Effect:
    return Effect(
        txn_id="txn-test",
        index=index,
        tool="t",
        args={"i": index},
        resource=resource,
        reversible=reversible,
        read_keys=list(read_keys or []),
        write_keys=list(write_keys or []),
    )


# --- check_conflicts ---------------------------------------------------------


def test_no_reads_no_conflicts():
    adapters = {"fake": FakeAdapter()}
    assert check_conflicts([_effect()], adapters) == []


def test_matching_versions_produce_no_conflict():
    adapter = FakeAdapter(versions={("k",): 5})
    eff = _effect(read_keys=[("fake", ("k",), 5)])
    assert check_conflicts([eff], {"fake": adapter}) == []


def test_moved_version_produces_conflict():
    adapter = FakeAdapter(versions={("k",): 6})
    eff = _effect(read_keys=[("fake", ("k",), 5)])
    conflicts = check_conflicts([eff], {"fake": adapter})
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c == Conflict(
        resource="fake",
        key=("k",),
        version_at_read=5,
        version_now=6,
        # Read-only key (no own write) → expected current is v_at_read.
        version_expected=5,
    )


def test_multiple_conflicts_are_all_reported():
    adapter = FakeAdapter(versions={("a",): 2, ("b",): 9, ("c",): 1})
    eff = _effect(
        read_keys=[
            ("fake", ("a",), 1),
            ("fake", ("b",), 1),
            ("fake", ("c",), 1),
        ]
    )
    conflicts = check_conflicts([eff], {"fake": adapter})
    keys = {c.key for c in conflicts}
    assert keys == {("a",), ("b",)}  # c matched; a and b moved


def test_non_rollback_adapter_is_skipped():
    """HTTP-style adapters are isolated-by-construction via staging — the
    diff must never invoke ``read_version`` on them (it would raise).
    """
    http_like = FakeAdapter(name="http", rollback_ok=False)
    # If read_version were called we'd see an error here — but the diff
    # must short-circuit on ``supports_rollback() is False``.
    http_like.read_version = lambda key: (_ for _ in ()).throw(  # pragma: no cover
        AssertionError("read_version called on non-rollback adapter")
    )
    eff = _effect(
        resource="http",
        reversible=False,
        read_keys=[("http", ("k",), 5)],
    )
    assert check_conflicts([eff], {"http": http_like}) == []


def test_unknown_resource_is_skipped_silently():
    """A read_key whose resource has no adapter is skipped — defensive only;
    the runtime should never put such an entry in the journal."""
    eff = _effect(read_keys=[("ghost", ("k",), 5)])
    assert check_conflicts([eff], {}) == []


def test_diff_walks_every_effect_in_the_journal():
    adapter = FakeAdapter(versions={("a",): 1, ("b",): 2, ("c",): 3})
    e0 = _effect(index=0, read_keys=[("fake", ("a",), 1)])
    e1 = _effect(index=1, read_keys=[("fake", ("b",), 1)])  # moved
    e2 = _effect(index=2, read_keys=[("fake", ("c",), 3)])
    conflicts = check_conflicts([e0, e1, e2], {"fake": adapter})
    assert [c.key for c in conflicts] == [("b",)]


def test_read_key_with_list_form_is_normalised_to_tuple():
    """Some recorders (e.g. the SQL helper) may pass a list — the
    Conflict's ``key`` field should still be a tuple for hashability."""
    adapter = FakeAdapter(versions={("k",): 6})
    eff = _effect(read_keys=[("fake", ["k"], 5)])
    conflicts = check_conflicts([eff], {"fake": adapter})
    assert conflicts[0].key == ("k",)
    assert isinstance(conflicts[0].key, tuple)


# --- Slice 4 P3: self-bump vs cross-txn disambiguation ----------------------


def test_self_bump_alone_does_not_flag_a_conflict():
    """If I read K@v=5, write K, and write_keys records v_after_write=6,
    then at commit time the live version IS 6 because of my own write —
    the diff must not flag this as a conflict.
    """
    adapter = FakeAdapter(versions={("k",): 6})  # post-my-write state
    eff = _effect(
        read_keys=[("fake", ("k",), 5)],
        write_keys=[("fake", ("k",), 6)],  # my expected-current after write
    )
    assert check_conflicts([eff], {"fake": adapter}) == []


def test_cross_txn_write_after_my_write_is_flagged_as_conflict():
    """The bug Slice 4's original code had: a txn that BOTH read AND
    wrote the same key would silently swallow cross-txn writes via the
    `own_writes` filter. With P3 the diff uses `last_my_write` to compute
    "my expected current" and flags anything beyond that as a conflict.

    Scenario: I read K@v=5, write K (expecting v=6 after), then someone
    else writes K (advancing live to v=7). My commit-time diff sees
    v_now=7 != my_expected=6 — conflict, correctly.
    """
    adapter = FakeAdapter(versions={("k",): 7})  # someone else also wrote
    eff = _effect(
        read_keys=[("fake", ("k",), 5)],
        write_keys=[("fake", ("k",), 6)],  # my expected-current after my write
    )
    conflicts = check_conflicts([eff], {"fake": adapter})
    assert len(conflicts) == 1
    assert conflicts[0].key == ("k",)
    assert conflicts[0].version_now == 7
    # version_at_read reports the version I observed on first read
    assert conflicts[0].version_at_read == 5


def test_multiple_writes_to_same_key_use_last_my_write_as_expected():
    """Repeated writes append; `last_my_write` picks the freshest. The
    expected current = the version after my LAST write of K.
    """
    adapter = FakeAdapter(versions={("k",): 8})  # matches my last bump
    eff = _effect(
        read_keys=[("fake", ("k",), 5)],
        write_keys=[
            ("fake", ("k",), 6),
            ("fake", ("k",), 7),
            ("fake", ("k",), 8),
        ],
    )
    # My last write produced v=8; live version is 8; no conflict.
    assert check_conflicts([eff], {"fake": adapter}) == []


def test_multiple_writes_then_cross_txn_write_flags_conflict():
    """Same as the multi-write case but a concurrent writer bumped K
    one more time after my last write. Must flag a conflict.
    """
    adapter = FakeAdapter(versions={("k",): 9})  # one beyond my last write
    eff = _effect(
        read_keys=[("fake", ("k",), 5)],
        write_keys=[
            ("fake", ("k",), 6),
            ("fake", ("k",), 7),
            ("fake", ("k",), 8),
        ],
    )
    conflicts = check_conflicts([eff], {"fake": adapter})
    assert len(conflicts) == 1
    assert conflicts[0].version_now == 9


def test_write_without_read_does_not_appear_in_diff():
    """The diff walks `read_keys`. A key I only WROTE (didn't read first)
    isn't checked — write-only effects don't participate in lost-update
    detection. This is the Slice-4 scope: read-write conflict, not
    write-write conflict.
    """
    adapter = FakeAdapter(versions={("k",): 99})  # massively moved
    eff = _effect(
        read_keys=[],  # no reads
        write_keys=[("fake", ("k",), 1)],  # I wrote once
    )
    assert check_conflicts([eff], {"fake": adapter}) == []


# --- IsolationConflict carry-through ----------------------------------------


def test_isolation_conflict_carries_conflicts_list():
    c = Conflict(resource="r", key=("k",), version_at_read=1, version_now=2)
    exc = IsolationConflict([c])
    assert exc.conflicts == [c]
    assert "r:('k',)" in str(exc)


def test_isolation_conflict_message_is_diagnostic():
    cs = [
        Conflict(resource="sql", key=("users", 7), version_at_read=2, version_now=3),
        Conflict(resource="fs", key=("a.txt",), version_at_read="X", version_now="Y"),
    ]
    msg = str(IsolationConflict(cs))
    assert "sql:('users', 7)" in msg
    assert "fs:('a.txt',)" in msg


# --- resolution policies (unit-level) ---------------------------------------


def test_abort_resolve_raises_isolation_conflict():
    cs = [Conflict(resource="r", key=("k",), version_at_read=1, version_now=2)]
    with pytest.raises(IsolationConflict) as info:
        Abort().resolve(None, cs)
    assert info.value.conflicts == cs


def test_retry_resolve_raises_internal_retry_signal_inside_run_txn():
    """Inside run_txn's contextvar window, Retry.resolve raises the
    internal _RetrySignal so run_txn's outer loop can catch and replay.
    Outside that window (covered by the next test), Retry.resolve
    degrades to a public IsolationConflict.
    """
    from pherix.core.isolation import _in_run_txn

    cs = [Conflict(resource="r", key=("k",), version_at_read=1, version_now=2)]
    token = _in_run_txn.set(True)
    try:
        with pytest.raises(_RetrySignal) as info:
            Retry(max_attempts=2).resolve(None, cs)
        assert info.value.conflicts == cs
    finally:
        _in_run_txn.reset(token)


def test_retry_resolve_outside_run_txn_degrades_to_isolation_conflict():
    """Outside run_txn (the contextvar is False — the default), Retry's
    resolve raises the public IsolationConflict rather than leaking the
    private _RetrySignal to a caller who isn't supposed to import it.
    """
    cs = [Conflict(resource="r", key=("k",), version_at_read=1, version_now=2)]
    with pytest.raises(IsolationConflict) as info:
        Retry(max_attempts=2).resolve(None, cs)
    assert info.value.conflicts == cs
    # And it's NOT the internal signal type.
    assert not isinstance(info.value, _RetrySignal)


def test_serialize_resolve_falls_back_to_isolation_conflict():
    # The actual wait happens in the runtime's commit path BEFORE resolve.
    # If resolve is reached the wait already finished and a conflict
    # remains — degrade to Abort behaviour.
    cs = [Conflict(resource="r", key=("k",), version_at_read=1, version_now=2)]
    with pytest.raises(IsolationConflict):
        Serialize(timeout_seconds=0.0).resolve(None, cs)


def test_retry_default_max_attempts_is_three():
    assert Retry().max_attempts == 3
