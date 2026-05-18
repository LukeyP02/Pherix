"""Slice 4 — isolation policies and the commit-time conflict diff.

Maths framing: the journal is a time series of observables; a conflict is
a non-commutativity event between this transaction's read set
{(resource, key, v_read)} and the committed mutations on those same keys
since the transaction opened. The resolution policy is a callable
``f: Conflict -> Action``, with ``Action`` drawn from
``{Abort, Retry, Serialize}``.

Per D3 the diff fires only at commit time — per-effect checking would
require a global lock over every operation. Reads within a transaction
are isolated by the journal's append-only semantics (a transaction reads
its own writes, untouched by others until commit), so there is no TOCTOU
window inside a txn — only at the commit boundary.

Per D5 Slice 4 is single-host: the arbitration substrate is either the
in-process :data:`REGISTRY` singleton (multi-agent in one Python process)
or the filesystem-shared adapter state (multi-process, single host —
multiple processes pointed at the same SQLite file see each other's
side-table version bumps through normal SQLite WAL semantics).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Conflict:
    """One non-commutativity event.

    ``version_at_read`` is the version recorded into ``Effect.read_keys`` at
    read-time; ``version_now`` is the value the adapter reports at commit
    time. They differ iff some other transaction committed a write to the
    same key between this txn opening and this txn's commit-time diff.
    """

    resource: str
    key: tuple
    version_at_read: object
    version_now: object


class IsolationConflict(RuntimeError):
    """Raised when the commit-time diff fires under :class:`Abort` policy.

    The list of conflicting keys is carried as :attr:`conflicts` so the
    operator (or an enclosing retry loop) can inspect what moved without
    re-parsing the message string.
    """

    def __init__(self, conflicts: list[Conflict]):
        self.conflicts = list(conflicts)
        lines = "; ".join(
            f"{c.resource}:{c.key} (read v{c.version_at_read!r}, "
            f"now v{c.version_now!r})"
            for c in conflicts
        )
        super().__init__(f"isolation conflict on {lines}")


class _RetrySignal(Exception):
    """Internal signal from :class:`Retry` to :func:`run_txn`.

    Not part of the public surface — :func:`run_txn` catches and loops on it;
    nothing else should. Carries the conflicts so the final IsolationConflict
    after exhaustion can report what kept moving.
    """

    def __init__(self, conflicts: list[Conflict]):
        self.conflicts = list(conflicts)
        super().__init__("isolation conflict (retry signal)")


# --- resolution policies -----------------------------------------------------


@dataclass
class Abort:
    """Default: raise :class:`IsolationConflict`; the txn unwinds normally.

    Maths framing: the trivial choice of ``f: Conflict -> Action`` — every
    conflict maps to "give up and tell the caller". The caller decides
    whether to retry; Pherix does not.
    """

    def resolve(self, ctx: Any, conflicts: list[Conflict]) -> None:
        raise IsolationConflict(conflicts)


@dataclass
class Retry:
    """Roll back and replay the transaction body up to ``max_attempts`` times.

    Only meaningful with the :func:`run_txn` entry point — re-entering a
    ``with agent_txn(...)`` block from outside Pherix is mechanically
    impossible (a context manager's body is not a callable Pherix owns).
    Used with the with-form, :class:`Retry` degrades to :class:`Abort`
    behaviour: ``IsolationConflict`` is raised on the first conflict and
    the operator should switch entry points.
    """

    max_attempts: int = 3

    def resolve(self, ctx: Any, conflicts: list[Conflict]) -> None:
        raise _RetrySignal(conflicts)


@dataclass
class Serialize:
    """Block this commit until no other in-flight txn writes any of our reads.

    Slice 4 limitation: in-process only. Multi-process Serialize is deferred
    (the Slice 8 gateway is the natural arbiter). With the filesystem-shared
    journal substrate Serialize cannot see other processes' in-flight write
    plans, so a cross-process conflict falls through to the diff and is
    reported as :class:`IsolationConflict` — i.e. Serialize degrades to
    Abort across process boundaries.

    The actual waiting is driven by the runtime BEFORE the diff fires. By
    the time :meth:`resolve` is called the wait has already finished AND
    a conflict still exists on the post-wait diff — so this is the
    unhappy-path fallback.
    """

    timeout_seconds: float = 30.0

    def resolve(self, ctx: Any, conflicts: list[Conflict]) -> None:
        # Fell through from the runtime's pre-diff wait; treat as Abort.
        raise IsolationConflict(conflicts)


# --- the diff ----------------------------------------------------------------


def check_conflicts(
    effects: list, adapters: dict
) -> list[Conflict]:
    """Fold the journal: ask each adapter for the current version of every
    read key; emit a :class:`Conflict` for any that has moved.

    Effects whose adapter is non-rollback (e.g. :class:`HTTPAdapter`) are
    skipped: irreversibles are isolated-by-construction via staging, so
    their reads (if any) do not participate in MVCC.

    Self-bump caveat: when this transaction *also* writes a key it read,
    the adapter's monotonic counter has been bumped by our own write —
    asking ``read_version`` at commit time would report ``v_at_read +
    my_bumps`` and mis-classify our own write as a conflict. Slice 4
    skips the diff on those keys: the version moving is consistent with
    being self-caused. A real lost-update where another txn ALSO wrote
    the same key cannot be distinguished from a pure self-bump under
    monotonic-counter versioning (the bookkeeping cost is paid in
    Stream A's note on D2; SSI or row-ctid in Postgres would close this
    gap). The canonical Slice 4 lost-update pin uses a read-only
    "loser" txn — its read-set diff still fires cleanly under
    first-committer-wins semantics, which is the property Slice 4
    guarantees.
    """
    own_writes: set[tuple] = {
        (r, tuple(k))
        for effect in effects
        for (r, k) in effect.write_keys
    }
    conflicts: list[Conflict] = []
    for effect in effects:
        for entry in effect.read_keys:
            resource, key, v_at_read = entry
            adapter = adapters.get(resource)
            if adapter is None or not adapter.supports_rollback():
                continue
            if (resource, tuple(key)) in own_writes:
                continue
            v_now = adapter.read_version(tuple(key))
            if v_now != v_at_read:
                conflicts.append(
                    Conflict(
                        resource=resource,
                        key=tuple(key),
                        version_at_read=v_at_read,
                        version_now=v_now,
                    )
                )
    return conflicts


# --- JournalRegistry (D5 in-process arbitration substrate) -------------------


class JournalRegistry:
    """In-process registry of open transactions for :class:`Serialize`.

    Singleton-style — :data:`REGISTRY`. Each :class:`TxnContext` registers
    itself on open and unregisters on close. :class:`Serialize` consults
    the registry to find other in-flight txns whose write_keys intersect
    this txn's read_keys and waits on their completion events before
    running the commit-time diff.

    Threading model: a single lock guards every read/write of registry
    state. The waiter pattern is:

        1. Take the lock; snapshot the set of open txns and the
           (resource, key) writes each plans on the in-process side.
        2. Compute the intersection with our read_keys; pick the txns
           we have to wait on; register events under their txn_ids;
           release the lock.
        3. Wait on each event (or timeout).
        4. Re-take the lock to clean up.

    Cross-host coordination is explicitly deferred to Slice 8 (the gateway
    is the natural arbiter); cross-process single-host Serialize falls
    back to the post-wait diff via the filesystem-shared SQLite side
    table — which sees other processes' COMMITTED bumps, not their
    in-flight plans.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, Any] = {}  # txn_id -> ctx
        # txn_id -> list[Event]: events fired when that txn closes.
        self._close_events: dict[str, list[threading.Event]] = {}

    # --- lifecycle ----------------------------------------------------------

    def register(self, ctx: Any) -> None:
        with self._lock:
            self._open[ctx.txn_id] = ctx

    def unregister(self, ctx: Any) -> None:
        with self._lock:
            self._open.pop(ctx.txn_id, None)
            events = self._close_events.pop(ctx.txn_id, [])
        # Fire outside the lock so a waiter's wake-up does not deadlock.
        for ev in events:
            ev.set()

    # --- introspection ------------------------------------------------------

    def open_contexts(self) -> list[Any]:
        with self._lock:
            return list(self._open.values())

    # --- Serialize coordination --------------------------------------------

    def wait_for_blockers(
        self,
        my_txn_id: str,
        my_read_keys: list[tuple],
        timeout_seconds: float,
    ) -> None:
        """Block until every other open txn whose planned writes intersect
        ``my_read_keys`` has closed (committed or rolled back) — or the
        timeout expires.

        After return, the caller re-runs :func:`check_conflicts`. A wait
        that wakes via timeout still falls through to the diff; the diff
        either finds the world quiet (no conflict — proceed) or still
        moving (Conflict raised under :class:`Serialize` degraded to
        Abort).
        """
        my_reads = {(r, tuple(k)) for (r, k, _v) in my_read_keys}
        if not my_reads:
            return

        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                blockers: list[tuple[str, threading.Event]] = []
                for txn_id, ctx in self._open.items():
                    if txn_id == my_txn_id:
                        continue
                    # Collect their planned writes from the journal — the
                    # effects list is append-only so this snapshot is
                    # consistent under our lock.
                    other_writes = {
                        (r, tuple(k))
                        for effect in ctx.txn.effects
                        for (r, k) in effect.write_keys
                    }
                    if my_reads & other_writes:
                        ev = threading.Event()
                        self._close_events.setdefault(txn_id, []).append(ev)
                        blockers.append((txn_id, ev))

            if not blockers:
                return

            for txn_id, ev in blockers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                ev.wait(timeout=remaining)
                # Loop: re-check (a fresh open txn may have appeared while
                # we waited, or the closed txn's writes may have been
                # rolled back — re-evaluation is the safe thing).


# Process-global singleton. Tests should call :meth:`unregister` for any
# ctx they register manually; the runtime's agent_txn handles this
# automatically.
REGISTRY = JournalRegistry()
