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

import contextvars
import threading
import time
from dataclasses import dataclass
from typing import Any


# Set to True by :func:`pherix.frontends.library.run_txn` for the duration of
# its retry loop. :meth:`Retry.resolve` checks this flag to decide whether to
# raise the internal :class:`_RetrySignal` (which ``run_txn`` catches and
# replays) or convert it to a public :class:`IsolationConflict` (which the
# ``with agent_txn(...)`` form's caller can legitimately handle). Without
# this gate the internal signal would leak out of ``agent_txn`` to a user
# who is told in the docs that Retry "degrades to Abort" when used with the
# context-manager form — the contract is now enforced.
_in_run_txn: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pherix_in_run_txn", default=False
)


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

    Only meaningful with the :func:`pherix.run_txn` entry point — re-entering
    a ``with agent_txn(...)`` block from outside Pherix is mechanically
    impossible (a context manager's body is not a callable Pherix owns).
    Used with the with-form, :class:`Retry` degrades to :class:`Abort`
    behaviour: :class:`IsolationConflict` is raised on the first conflict
    and the operator should switch entry points. The :data:`_in_run_txn`
    contextvar is what selects between the two paths — set by ``run_txn``
    for the duration of its loop, default ``False`` everywhere else.

    Idempotency caveat: a Retry replay re-runs the user's ``fn`` from the
    top against a fresh :class:`TxnContext`. Anything Pherix journals
    (SQL via :func:`execute_isolated`, FS via :class:`FsHandle`, HTTP
    staged effects) is properly unwound between attempts. Anything the
    function does *outside* Pherix's seam — appending to a module-level
    list, writing a file with raw ``open()``, firing an unwrapped HTTP
    request, mutating a closure variable — fires on *every* attempt. The
    same constraint applies to a hand-rolled retry loop around
    :class:`Abort`; Pherix does not promise more than the journal can see.
    """

    max_attempts: int = 3

    def resolve(self, ctx: Any, conflicts: list[Conflict]) -> None:
        if _in_run_txn.get():
            # Inside run_txn: signal a replay attempt. run_txn's outer
            # loop catches this, rolls the txn back via agent_txn's
            # normal unwind path, and re-invokes fn against a fresh
            # TxnContext.
            raise _RetrySignal(conflicts)
        # Outside run_txn (i.e. inside a bare ``with agent_txn(...)``):
        # there's no callable Pherix can re-invoke, so degrade to Abort
        # cleanly rather than leak the internal _RetrySignal name to a
        # caller who isn't supposed to know it exists.
        raise IsolationConflict(conflicts)


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
    """Fold the journal: for every read key, compare the version we expect
    against the version the adapter reports now. Emit a :class:`Conflict`
    when those differ.

    Effects whose adapter is non-rollback (e.g. :class:`HTTPAdapter`) are
    skipped: irreversibles are isolated-by-construction via staging, so
    their reads (if any) do not participate in MVCC.

    Self-bump disambiguation (Slice 4 P3 follow-up). When a transaction
    reads a key and then writes it, the adapter's version moves because
    of OUR own write. Previously the diff filtered such keys out entirely
    via an ``own_writes`` set — but that filter was too permissive: a
    genuine lost-update where another transaction ALSO bumped the same
    key was silently swallowed alongside the self-bump.

    The fix: ``write_keys`` triples carry ``(resource, key,
    version_after_my_write)``. For each read key, we compute "my expected
    current" as the version after my LAST write of that key (or, if I
    didn't write the key, the version when I read it). The diff fires
    when the adapter's live version is anything other than that. The
    structure is monotonic — my own bumps move the expected current
    upward consistently; only a cross-transaction write moves the live
    version past it.
    """
    # Per (resource, key): the version produced by my LAST write. Iteration
    # order in ``effect.write_keys`` is append-order, and write_keys is
    # populated in time order by the resource handles, so the last entry
    # for a given key is the freshest post-write version we produced.
    last_my_write: dict[tuple, object] = {}
    for effect in effects:
        for entry in effect.write_keys:
            resource, key, v_after = entry
            last_my_write[(resource, tuple(key))] = v_after

    conflicts: list[Conflict] = []
    for effect in effects:
        for entry in effect.read_keys:
            resource, key, v_at_read = entry
            adapter = adapters.get(resource)
            if adapter is None or not adapter.supports_rollback():
                continue
            key_tuple = tuple(key)
            # "Expected current" per key:
            # - If I wrote the key after reading it: the version after my
            #   last write (we expect the adapter to still report that).
            # - If I only read the key: v_at_read.
            v_expected = last_my_write.get((resource, key_tuple), v_at_read)
            v_now = adapter.read_version(key_tuple)
            if v_now != v_expected:
                conflicts.append(
                    Conflict(
                        resource=resource,
                        key=key_tuple,
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
                        for (r, k, _v) in effect.write_keys
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
