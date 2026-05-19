"""The orchestration — agent_txn() and the interception entry point.

``agent_txn()`` opens a :class:`Transaction`, binds a :class:`TxnContext` into
the ``active_txn`` ContextVar, and drives every intercepted tool call through
the right lane:

- **reversible lane (Slices 1 + 2):** policy -> snapshot -> apply -> journal.
  Effects run live; ``rollback()`` folds the journal backward, restoring each
  snapshot newest-first.
- **irreversible lane (Slice 3):** policy -> stage. The effect is recorded as
  intent and the agent receives a ``StagedResult(effect_id=...)`` sentinel.
  ``commit()`` re-checks policy (D4 TOCTOU), checks the gate (every staged
  irreversible must be compensator-backed or pre-approved via
  :meth:`TxnContext.approve_irreversible`), then fires staged irreversibles
  in journal index order. A mid-fire failure triggers a *mixed-fold* backward
  unwind: ``compensator(effect)`` for already-fired irreversibles,
  ``adapter.restore(snapshot)`` for already-applied reversibles. Terminal
  state is ``ROLLED_BACK`` if every step of the unwind succeeded; ``STUCK``
  if any compensator was missing or itself raised.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from pherix.core.adapters.base import TransactionalResourceAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus, StagedResult
from pherix.core.isolation import (
    REGISTRY as ISOLATION_REGISTRY,
    Abort,
    Serialize,
    _RetrySignal,
    check_conflicts,
)
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.tools import REGISTRY, active_effect, active_txn
from pherix.core.transaction import Transaction, TransactionStateError, TxnState


class CompensatorNotRegistered(RuntimeError):
    """Raised at stage-time when a tool declares a compensator that does not exist.

    The journal stores compensator names (strings); the registry resolves
    names to callables at fire-time. Catching the typo at stage-time turns a
    silent STUCK-on-rollback into a loud error before any state changes.
    """

    def __init__(self, compensator: str, tool: str):
        self.compensator = compensator
        self.tool = tool
        super().__init__(
            f"tool {tool!r} declares compensator {compensator!r}, but no tool "
            f"of that name is registered. The compensator must itself be a "
            f"registered @tool."
        )


class GateBlocked(RuntimeError):
    """Raised at commit-time when staged irreversibles need pre-approval.

    Carries the list of effect_ids still requiring
    :meth:`TxnContext.approve_irreversible`. After a gate-block the
    transaction is unwound (reversibles restored, irreversibles untouched)
    and ends in ``ROLLED_BACK``.
    """

    def __init__(self, needs_approval: list[str]):
        self.needs_approval = list(needs_approval)
        super().__init__(
            "commit blocked at the gate; the following staged irreversible "
            "effects need approve_irreversible() or a registered compensator: "
            + ", ".join(self.needs_approval)
        )


def _unique(adapters: dict[str, Any]) -> list[Any]:
    """Distinct adapter instances (one adapter may serve several resource keys)."""
    seen: set[int] = set()
    out: list[Any] = []
    for a in adapters.values():
        if id(a) not in seen:
            seen.add(id(a))
            out.append(a)
    return out


class TxnContext:
    """The active-transaction object stored in the ``active_txn`` ContextVar.

    The ``@tool`` wrapper calls :meth:`record_tool_call` on this — it is the
    runtime's single interception entry point.
    """

    def __init__(
        self,
        adapters: dict[str, Any],
        policy: Policy,
        audit: AuditJournal,
        isolation: Any = None,
    ):
        self.txn = Transaction(policy=policy)
        self.audit = audit
        self._adapters = adapters
        self._policy = policy
        # Slice 4 (D4): the resolution policy is a callable
        # ``f: Conflict -> Action`` chosen per transaction. Default is
        # :class:`Abort` — the most permissive failure mode (raise and let
        # the caller decide).
        self._isolation = isolation if isolation is not None else Abort()
        self._owner_thread = threading.get_ident()
        self._finished = False
        # Pre-approval tokens for staged irreversibles, keyed by effect_id.
        # Recorded by approve_irreversible(); consumed by the commit-time gate.
        self._approvals: set[str] = set()
        audit.record_transaction(self.txn)

    @property
    def txn_id(self) -> str:
        return self.txn.txn_id

    # --- interception ---

    def record_tool_call(self, tool_name: str, args: tuple, kwargs: dict) -> Any:
        self._guard_thread()
        self._guard_open()
        spec = REGISTRY.get(tool_name)

        # Stage-time policy check (D6). On denial nothing is appended to the
        # journal and no resource is touched — the violation simply propagates.
        self._policy.check(tool_name)

        # Stage-time compensator-name validation (D2). Catches typos before
        # any state changes; the journal stores the resolved name.
        if spec.compensator is not None and spec.compensator not in REGISTRY:
            raise CompensatorNotRegistered(spec.compensator, tool_name)

        adapter = self._resolve_adapter(spec.resource)
        effect = Effect(
            txn_id=self.txn.txn_id,
            index=self.txn.next_index(),
            tool=tool_name,
            args=spec.bind_args(args, kwargs),
            resource=spec.resource,
            reversible=adapter.supports_rollback(),
            compensator=spec.compensator,
        )
        self.txn.add_effect(effect)
        self.audit.record_effect(effect)

        if not effect.reversible:
            # Staging lane (Slice 3): no snapshot, no live apply. The effect
            # exists in the journal as intent; the agent gets a sentinel
            # carrying the deterministic effect_id. The real fire happens at
            # commit-time.
            result = StagedResult(effect_id=effect.effect_id)
            effect.result = result
            # status remains STAGED (the dataclass default) — make it explicit
            # so the audit row reflects the same fact.
            effect.status = EffectStatus.STAGED
            self.audit.update_effect(effect)
            return result

        # Reversible lane (Slices 1 + 2): snapshot precedes apply, so even a
        # failing apply leaves a restorable before-state and rollback is
        # always clean.
        effect.snapshot = adapter.snapshot(effect)
        # Slice 4: bind the effect into the ``active_effect`` ContextVar so
        # resource handles (FsHandle, ``execute_isolated``) can record
        # read_keys / write_keys without an explicit parameter on every tool.
        token = active_effect.set(effect)
        try:
            effect.result = adapter.apply(effect, spec.fn)
        except Exception:
            effect.status = EffectStatus.FAILED
            self.audit.update_effect(effect)
            raise
        finally:
            active_effect.reset(token)
        effect.status = EffectStatus.APPLIED
        self.audit.update_effect(effect)
        return effect.result

    # --- approval (D3) ---

    def approve_irreversible(self, effect_id: str) -> None:
        """Record out-of-band pre-approval for one staged irreversible effect.

        D3: the verdict is *recorded*, not *generated* — Pherix never
        decides for itself whether an irreversible effect should fire. A
        human (or another agent with authority, or a deterministic
        guardrail) calls this for each staged irreversible that lacks a
        compensator. At commit, every staged irreversible must be either
        auto-committable (compensator registered) OR pre-approved here,
        else the gate blocks.

        Approving an unknown ``effect_id`` raises — silent acceptance would
        let typos slip through to a gate-block surprise.
        """
        self._guard_thread()
        self._guard_open()
        if not any(e.effect_id == effect_id for e in self.txn.effects):
            raise ValueError(
                f"no staged effect with effect_id {effect_id!r} in transaction "
                f"{self.txn.txn_id}"
            )
        self._approvals.add(effect_id)

    # --- finalisation ---

    def commit(self) -> None:
        self._guard_thread()
        self._guard_open()

        # Slice 4 (D3): conflict detection runs at commit-time only. Reads
        # within a txn are isolated by the journal's append-only semantics,
        # so the only window where a concurrent commit can have moved one
        # of our read versions is between this txn's open and its commit.
        # The diff is a backward fold against the *current* adapter state.
        self._run_isolation_check()

        staged = [
            e for e in self.txn.effects
            if e.status is EffectStatus.STAGED and not e.reversible
        ]

        if staged:
            # OPEN -> STAGED. The transition itself uses the state machine,
            # so an illegal mid-commit re-entry would raise here.
            self.txn.transition(TxnState.STAGED)
            self.audit.update_transaction_state(
                self.txn.txn_id, TxnState.STAGED.name
            )

            # D4: re-evaluate stage-time policy at commit start (TOCTOU). For
            # Slice 1's stateless allow-list this is trivially equal to the
            # stage-time evaluation — the hook lives in the right place for
            # Slice 6's state-dependent policy.
            for e in staged:
                try:
                    self._policy.check(e.tool)
                except PolicyViolation:
                    e.status = EffectStatus.GATED
                    self.audit.update_effect(e)
                    # Unwind reversibles, then propagate. Irreversibles never
                    # fired, so there are no compensators to run.
                    self._partial_unwind()
                    raise

            # D3: the gate — every staged irreversible must be
            # compensator-backed OR pre-approved.
            needs_approval = [
                e.effect_id
                for e in staged
                if e.compensator is None and e.effect_id not in self._approvals
            ]
            if needs_approval:
                for e in staged:
                    if (
                        e.compensator is None
                        and e.effect_id not in self._approvals
                    ):
                        e.status = EffectStatus.GATED
                        self.audit.update_effect(e)
                self._partial_unwind()
                raise GateBlocked(needs_approval)

            # D5: forward fold over staged irreversibles. A mid-fire failure
            # triggers the mixed-fold backward unwind.
            for e in staged:
                if e.status is EffectStatus.APPLIED:
                    # Idempotency by effect_id: a re-fire of an already-
                    # applied effect is a no-op. (Cannot happen on the first
                    # pass, but the property must hold for any future
                    # re-entry — e.g. replay in Slice 5.)
                    continue
                adapter = self._resolve_adapter(e.resource)
                spec = REGISTRY.get(e.tool)
                # Slice 4: bind the effect for read/write-key capture, even
                # in the irreversible lane. Strictly redundant for the
                # HTTPAdapter (it doesn't participate in MVCC) but keeps the
                # contextvar consistent across both lanes — any future
                # adapter that stages but still wants per-effect bookkeeping
                # gets it for free.
                token = active_effect.set(e)
                try:
                    e.result = adapter.apply(e, spec.fn)
                except Exception:
                    e.status = EffectStatus.FAILED
                    self.audit.update_effect(e)
                    self._partial_unwind()
                    raise
                finally:
                    active_effect.reset(token)
                e.status = EffectStatus.APPLIED
                self.audit.update_effect(e)

        # Finalise — commit transactional adapters (SQL etc.). For staged
        # commits this is the COMMITTED-from-STAGED transition; for pure
        # reversible commits it is the COMMITTED-from-OPEN transition.
        for adapter in _unique(self._adapters):
            if isinstance(adapter, TransactionalResourceAdapter):
                adapter.commit()
        self.txn.transition(TxnState.COMMITTED)
        self.audit.update_transaction_state(self.txn.txn_id, TxnState.COMMITTED.name)
        self._finished = True

    # --- isolation (Slice 4) ---

    def _run_isolation_check(self) -> None:
        """Commit-time conflict diff (D3) + resolution dispatch (D4).

        For :class:`Serialize`: first wait — block this commit until no
        other in-flight in-process txn writes any of our read_keys (or the
        configured timeout expires). Then run the diff once on the
        post-wait world; if it is clean, return. If it still flags a
        conflict, fall through to the policy's :meth:`resolve` (which for
        Serialize degrades to :class:`Abort`-style :class:`IsolationConflict`).

        For :class:`Abort` and :class:`Retry`: no wait, just diff and
        dispatch. :class:`Abort` raises :class:`IsolationConflict`;
        :class:`Retry` raises :class:`_RetrySignal` for :func:`run_txn` to
        catch and replay.
        """
        # Collect this txn's read_keys from the journal up front; Serialize
        # needs them BEFORE the diff to know who to wait on.
        my_read_keys = [
            entry
            for effect in self.txn.effects
            for entry in effect.read_keys
        ]

        if isinstance(self._isolation, Serialize):
            ISOLATION_REGISTRY.wait_for_blockers(
                my_txn_id=self.txn.txn_id,
                my_read_keys=my_read_keys,
                timeout_seconds=self._isolation.timeout_seconds,
            )

        conflicts = check_conflicts(self.txn.effects, self._adapters)
        if not conflicts:
            return
        # Hands off to the policy. Abort raises IsolationConflict; Retry
        # raises _RetrySignal; Serialize raises IsolationConflict as the
        # last-resort fallback (the pre-diff wait already happened).
        self._isolation.resolve(self, conflicts)

    def rollback(self) -> None:
        self._guard_thread()
        self._guard_open()
        # Backward fold from OPEN: restore each reversible effect newest-first.
        # Staged irreversibles have never fired and have no snapshot — they
        # simply remain in the journal with status STAGED, the strongest
        # containment property Pherix offers: nothing irreversible happened.
        for effect in reversed(self.txn.effects):
            if effect.snapshot is None:
                continue
            adapter = self._resolve_adapter(effect.resource)
            adapter.restore(effect.snapshot)
            if effect.status is EffectStatus.APPLIED:
                effect.status = EffectStatus.COMPENSATED
                self.audit.update_effect(effect)
        for adapter in _unique(self._adapters):
            if isinstance(adapter, TransactionalResourceAdapter):
                adapter.rollback()
        self.txn.transition(TxnState.ROLLED_BACK)
        self.audit.update_transaction_state(
            self.txn.txn_id, TxnState.ROLLED_BACK.name
        )
        self._finished = True

    # --- recovery (D5) ---

    def _partial_unwind(self) -> None:
        """Mixed-fold backward unwind after a commit-time failure.

        Walks the journal backward. For each effect:
          - status APPLIED + reversible: ``adapter.restore(snapshot)``;
            status flips to COMPENSATED.
          - status APPLIED + irreversible: invoke the registered compensator
            tool with the effect's original args; status flips to
            COMPENSATED on success. A missing or failing compensator marks
            the transaction STUCK.
          - any other status (STAGED, GATED, FAILED, COMPENSATED): skip.
            Staged irreversibles never fired; FAILED is the one that
            triggered the unwind.

        If every step succeeds, the transaction lands in ROLLED_BACK and
        transactional adapters (SQL etc.) are rolled back too. If any
        compensator was missing or itself raised, the transaction lands in
        STUCK and transactional adapters are *also* rolled back: the
        operator's job is to manually re-attempt the missing compensator
        against the real-world artefacts the journal still describes.
        """
        self.txn.transition(TxnState.PARTIAL)
        self.audit.update_transaction_state(
            self.txn.txn_id, TxnState.PARTIAL.name
        )

        stuck = False
        for effect in reversed(self.txn.effects):
            if effect.status is not EffectStatus.APPLIED:
                continue

            if effect.reversible:
                # Reversible: restore from snapshot — same engine Slice 1 uses.
                adapter = self._resolve_adapter(effect.resource)
                adapter.restore(effect.snapshot)
                effect.status = EffectStatus.COMPENSATED
                self.audit.update_effect(effect)
                continue

            # Irreversible: invoke the registered compensator. A missing or
            # raising compensator leaves the effect APPLIED in the journal
            # (the operator needs that record to recover manually) and
            # marks the txn STUCK.
            if effect.compensator is None or effect.compensator not in REGISTRY:
                stuck = True
                continue
            comp_spec = REGISTRY.get(effect.compensator)
            comp_adapter = self._resolve_adapter(comp_spec.resource)
            # Synthetic effect for the compensator fire: not part of the
            # journal (no index, never persisted as a separate row), just
            # the carrier that adapter.apply expects.
            comp_effect = Effect(
                txn_id=self.txn.txn_id,
                index=-1,
                tool=effect.compensator,
                args=effect.args,
                resource=comp_spec.resource,
                reversible=False,
            )
            try:
                comp_adapter.apply(comp_effect, comp_spec.fn)
            except Exception:
                stuck = True
                continue
            effect.status = EffectStatus.COMPENSATED
            self.audit.update_effect(effect)

        # Roll back transactional adapters regardless: the SQL/FS side must
        # not leak into the committed world even on a STUCK txn, because the
        # operator's recovery target is the irreversible-only journal.
        for adapter in _unique(self._adapters):
            if isinstance(adapter, TransactionalResourceAdapter):
                adapter.rollback()

        if stuck:
            self.txn.transition(TxnState.STUCK)
            self.audit.update_transaction_state(
                self.txn.txn_id, TxnState.STUCK.name
            )
        else:
            self.txn.transition(TxnState.ROLLED_BACK)
            self.audit.update_transaction_state(
                self.txn.txn_id, TxnState.ROLLED_BACK.name
            )
        self._finished = True

    # --- guards ---

    def _guard_open(self) -> None:
        if self._finished or not self.txn.is_open:
            raise TransactionStateError(
                f"transaction {self.txn.txn_id} is already finished "
                f"({self.txn.state.name})"
            )

    def _guard_thread(self) -> None:
        # contextvars do not propagate across threads / process pools. Rather
        # than let interception silently miss (tool runs raw, un-journalled),
        # any cross-thread use of an open transaction fails loudly here.
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "Pherix transaction used from a different thread than the one "
                "that opened it; the active_txn ContextVar and the resource "
                "connections are not safe to share across threads."
            )

    def _resolve_adapter(self, resource: str) -> Any:
        try:
            return self._adapters[resource]
        except KeyError:
            raise RuntimeError(
                f"no adapter registered for resource {resource!r}"
            ) from None


@contextmanager
def agent_txn(
    adapters: dict[str, Any],
    policy: Policy | None = None,
    audit: AuditJournal | None = None,
    isolation: Any = None,
) -> Iterator[TxnContext]:
    """Wrap an agent's tool-call layer in a transaction.

    On a clean exit the transaction auto-commits; on an exception it
    auto-rolls-back and re-raises. ``commit()`` / ``rollback()`` may also be
    called explicitly on the yielded context for mid-sequence control.

    ``isolation`` (Slice 4 D4) is the resolution policy applied at commit
    when the read-set diff flags a conflict — one of :class:`Abort` (the
    default), :class:`Retry` (only meaningful with :func:`run_txn`), or
    :class:`Serialize`. The isolation diff itself runs unconditionally at
    commit-start; the policy decides what to do with conflicts.
    """
    policy = policy or Policy.allow_all()
    audit = audit or AuditJournal.in_memory()

    for adapter in _unique(adapters):
        if isinstance(adapter, TransactionalResourceAdapter):
            adapter.begin()

    ctx = TxnContext(adapters, policy, audit, isolation=isolation)
    # Slice 4 (D5): register the open ctx with the in-process arbitration
    # substrate so a concurrent Serialize commit can find us and wait.
    ISOLATION_REGISTRY.register(ctx)
    token = active_txn.set(ctx)
    try:
        try:
            yield ctx
            # Move the auto-commit inside the try block so an isolation
            # conflict raised by commit() falls into the except branch
            # below — the runtime rolls back cleanly via the existing
            # machinery before propagating the exception.
            if not ctx._finished:
                ctx.commit()
        except Exception:
            if not ctx._finished:
                ctx.rollback()
            raise
        finally:
            active_txn.reset(token)
    finally:
        # Unregister AFTER active_txn reset and AFTER rollback/commit have
        # run — so the close-event fires only once the txn is truly done
        # and no Serialize waiter wakes up on a still-in-flight state.
        ISOLATION_REGISTRY.unregister(ctx)
