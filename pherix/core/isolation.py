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
    time. ``version_expected`` is what the diff actually compared
    ``version_now`` against — the committed base at read on the
    committed-only path, or the version after my last own write
    (``last_my_write``) on the own-write-visible path. The conflict fired
    because ``version_now != version_expected``.

    Carrying ``version_expected`` is what makes a future false-positive
    self-explaining: the old message read "read v0, now v0" (a self-bump
    on the committed-only path looked like a conflict because the diff
    compared against ``last_my_write`` instead of the committed base),
    which gave no hint of what was actually being compared.
    """

    resource: str
    key: tuple
    version_at_read: object
    version_now: object
    version_expected: object = None


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
            f"expected v{c.version_expected!r}, now v{c.version_now!r})"
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

    Single-host coordination, two composed layers (#8):

    - **In-process** — the :data:`REGISTRY` singleton holds live ``TxnContext``
      objects; a waiter blocks on per-blocker :class:`threading.Event`\\ s.
    - **Cross-process** — for adapters backed by a shareable on-disk SQLite
      file, in-flight write plans are published as INTENTS into a shared
      ``_pherix_intents`` side-table in that same file. A waiter polls the
      table (backoff up to the timeout) and blocks while a conflicting live
      intent from another process exists; it proceeds once that intent
      clears (the writer committed/rolled back) — or the timeout expires,
      at which point it falls through to the committed-state diff (degrading
      to Abort, the honest fallback).

    Cross-HOST Serialize is still out of scope — that needs the #12 control
    plane as arbiter. Within one host (one on-disk DB), Serialize now waits
    on both same-process Events and other-process intents.

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

    The expected-current computation depends on whether the adapter's
    ``read_version`` reflects this txn's own uncommitted writes — the two
    SQLite paths disagree, and a fix that is correct on one is wrong on
    the other unless reconciled (the on-disk read-then-write false
    conflict, "read v0, now v0"):

    - **Own-write-visible** path (``:memory:`` SQLite main connection, the
      :class:`FakeAdapter`, FS): ``read_version`` sees my own bumps, so my
      expected current is the version after my LAST write of that key —
      ``last_my_write`` — (or, if I only read the key, ``v_at_read``). A
      live version beyond that means a cross-txn write. This is monotonic:
      my bumps move the expected current up consistently; only another
      transaction moves the live version past it.

    - **Committed-only** path (on-disk SQLite, via the meta connection):
      ``read_version`` does NOT see my own uncommitted writes — at read
      time AND at commit time. My self-bumps therefore cancel on both
      ends, and the correct comparison is the committed base at read
      (``v_at_read``) vs the committed base now (``v_now``). Using
      ``last_my_write`` here is the bug: ``last_my_write`` lives on the
      main-connection scale (it counts my bumps), but ``v_now`` does not,
      so they differ by my own writes and fire a spurious conflict.

    An adapter signals the committed-only path via ``reads_committed_only()``;
    adapters that don't expose it default to the own-write-visible branch,
    so the :class:`FakeAdapter` unit pins and the FS adapter are unchanged.
    Either way the invariant is identical: a conflict means *another*
    transaction committed a write to a key I read between my read and my
    commit — my own pre-commit writes never count.
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
            # "Expected current" per key — see the docstring. On the
            # committed-only path my own writes are invisible to
            # read_version on both ends, so the committed base at read
            # (v_at_read) is what the committed base now must still equal.
            # On the own-write-visible path read_version reflects my bumps,
            # so the expected current is the version after my last write.
            if _reads_committed_only(adapter):
                v_expected = v_at_read
            else:
                v_expected = last_my_write.get((resource, key_tuple), v_at_read)
            v_now = adapter.read_version(key_tuple)
            if v_now != v_expected:
                conflicts.append(
                    Conflict(
                        resource=resource,
                        key=key_tuple,
                        version_at_read=v_at_read,
                        version_now=v_now,
                        version_expected=v_expected,
                    )
                )
    return conflicts


def _reads_committed_only(adapter: Any) -> bool:
    """Whether ``adapter.read_version`` excludes this txn's own uncommitted
    writes. Adapters that don't expose ``reads_committed_only`` default to
    ``False`` (the own-write-visible branch) — so the FakeAdapter unit pins
    and the filesystem adapter keep their existing ``last_my_write``
    semantics with no change.
    """
    probe = getattr(adapter, "reads_committed_only", None)
    return bool(probe()) if callable(probe) else False


# --- cross-process intent coordination (#8, single-host tier) ----------------


def _intent_adapters(ctx: Any) -> list[Any]:
    """Distinct adapters on ``ctx`` that speak the intent-coordination
    protocol (``publish_intent`` / ``clear_intents`` / ``conflicting_intents``).

    Duck-typed rather than ``isinstance``-checked so :mod:`isolation` never
    imports :mod:`adapters.sql` (no cycle) and so any future adapter that
    implements the same three methods participates for free. Returns an empty
    list for ``None`` (a ctx not in the registry) or a ctx with no such
    adapter — both reduce :meth:`wait_for_blockers` to the in-process path.
    """
    if ctx is None:
        return []
    adapters = getattr(ctx, "_adapters", None)
    if not adapters:
        return []
    seen: set[int] = set()
    out: list[Any] = []
    for a in adapters.values():
        if id(a) in seen:
            continue
        if all(
            callable(getattr(a, m, None))
            for m in ("publish_intent", "clear_intents", "conflicting_intents")
        ):
            seen.add(id(a))
            out.append(a)
    return out


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

    Cross-PROCESS single-host coordination (#8) composes on top: when the
    waiter's adapters include a SQLite adapter backed by a shareable on-disk
    DB, :meth:`wait_for_blockers` ALSO polls the shared ``_pherix_intents``
    side-table for live write intents published by txns in other processes,
    and blocks while a conflicting intent exists. The two layers compose —
    a Serialize commit waits on both same-process Events AND other-process
    intents. Cross-HOST coordination is still deferred to the #12 control
    plane (the natural arbiter across hosts).
    """

    # Poll interval for the cross-process intent table, with a small backoff
    # cap. Short enough that a freed intent is noticed promptly; long enough
    # that a healthy wait does not hammer the DB.
    _POLL_MIN = 0.005
    _POLL_MAX = 0.05

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
        # #8: clear this txn's cross-process write intents so a Serialize
        # waiter in another process wakes and proceeds. Done here (not in
        # commit/rollback) because unregister is the single finalisation
        # seam the runtime AND dry_run already call — covers commit,
        # rollback, gate-block, and partial-unwind alike with no runtime
        # change. Best-effort: a clear failure must not break finalisation.
        for adapter in _intent_adapters(ctx):
            try:
                adapter.clear_intents(ctx.txn_id)
            except Exception:
                pass
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
        """Block until no other in-flight txn — in THIS process or any other
        process sharing the on-disk SQLite file — has a write plan that
        intersects ``my_read_keys``, or the timeout expires.

        Two composed layers (#8):

        - **In-process:** other live :class:`TxnContext`\\ s in
          :attr:`_open` whose journalled ``write_keys`` hit my reads — waited
          on via per-blocker :class:`threading.Event`\\ s (the fast path).
        - **Cross-process:** other processes' published write INTENTS in the
          shared ``_pherix_intents`` side-table — polled with backoff. Only
          consulted if my own adapters include a cross-process-capable
          (on-disk) SQLite adapter; in-memory-only txns skip this layer.

        After return, the caller re-runs :func:`check_conflicts`. A wait that
        wakes via timeout still falls through to the diff; the diff either
        finds the world quiet (no conflict — proceed) or still moving
        (Conflict raised under :class:`Serialize` degraded to Abort).
        """
        my_reads = {(r, tuple(k)) for (r, k, _v) in my_read_keys}
        if not my_reads:
            return

        # Cross-process intent adapters of the WAITING txn (looked up by id).
        # We poll each for foreign live intents on our read keys. Adapters
        # backed by an in-memory DB report no cross-process capability and
        # are skipped — the in-process Event layer covers them.
        with self._lock:
            my_ctx = self._open.get(my_txn_id)
        xproc_adapters = [
            a
            for a in _intent_adapters(my_ctx)
            if getattr(a, "supports_cross_process_intents", lambda: False)()
        ]

        deadline = time.monotonic() + timeout_seconds
        poll = self._POLL_MIN
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

            # Cross-process layer: any foreign live intent on my read keys?
            xproc_blocked = any(
                a.conflicting_intents(my_txn_id, my_reads)
                for a in xproc_adapters
            )

            if not blockers and not xproc_blocked:
                return

            # In-process blockers: wait on their close events (cheap, exact).
            for txn_id, ev in blockers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                ev.wait(timeout=remaining)

            # Cross-process blockers: no event to wait on across the process
            # boundary, so poll the intent table with capped backoff until
            # the intent clears or the deadline passes.
            if xproc_blocked:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(poll, remaining))
                poll = min(poll * 2, self._POLL_MAX)
            # Loop: re-check (a fresh txn may have appeared, an in-process
            # blocker's writes may have rolled back, or a foreign intent may
            # have cleared — re-evaluation is the safe thing).


# Process-global singleton. Tests should call :meth:`unregister` for any
# ctx they register manually; the runtime's agent_txn handles this
# automatically.
REGISTRY = JournalRegistry()
