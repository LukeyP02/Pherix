"""The orchestration — agent_txn() and the interception entry point.

``agent_txn()`` opens a :class:`Transaction`, binds a :class:`TxnContext` into
the ``active_txn`` ContextVar, and drives every intercepted tool call through
policy -> snapshot -> apply -> journal. ``commit()`` folds the journal forward
(finalising the resource transactions); ``rollback()`` folds it backward,
restoring each effect newest-first (D4).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.policy import Policy
from pherix.core.tools import REGISTRY, active_txn
from pherix.core.transaction import Transaction, TransactionStateError, TxnState


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
        self, adapters: dict[str, Any], policy: Policy, audit: AuditJournal
    ):
        self.txn = Transaction(policy=policy)
        self.audit = audit
        self._adapters = adapters
        self._policy = policy
        self._owner_thread = threading.get_ident()
        self._finished = False
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

        adapter = self._resolve_adapter(spec.resource)
        effect = Effect(
            txn_id=self.txn.txn_id,
            index=self.txn.next_index(),
            tool=tool_name,
            args=spec.bind_args(args, kwargs),
            resource=spec.resource,
            reversible=adapter.supports_rollback(),
        )
        self.txn.add_effect(effect)
        self.audit.record_effect(effect)

        # Snapshot precedes apply: even a failing apply leaves a restorable
        # before-state, so rollback is always clean.
        effect.snapshot = adapter.snapshot(effect)
        try:
            effect.result = adapter.apply(effect, spec.fn)
        except Exception:
            effect.status = EffectStatus.FAILED
            self.audit.update_effect(effect)
            raise
        effect.status = EffectStatus.APPLIED
        self.audit.update_effect(effect)
        return effect.result

    # --- finalisation ---

    def commit(self) -> None:
        self._guard_thread()
        self._guard_open()
        for adapter in _unique(self._adapters):
            if hasattr(adapter, "commit"):
                adapter.commit()
        self.txn.transition(TxnState.COMMITTED)
        self.audit.update_transaction_state(self.txn.txn_id, TxnState.COMMITTED.name)
        self._finished = True

    def rollback(self) -> None:
        self._guard_thread()
        self._guard_open()
        # Backward fold: restore each effect newest-first (D4). This is the
        # universal engine Slices 2-3 need — not a single-ROLLBACK shortcut.
        for effect in reversed(self.txn.effects):
            if effect.snapshot is None:
                continue
            adapter = self._resolve_adapter(effect.resource)
            adapter.restore(effect.snapshot)
            if effect.status is EffectStatus.APPLIED:
                effect.status = EffectStatus.COMPENSATED
                self.audit.update_effect(effect)
        for adapter in _unique(self._adapters):
            if hasattr(adapter, "rollback"):
                adapter.rollback()
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
) -> Iterator[TxnContext]:
    """Wrap an agent's tool-call layer in a transaction.

    On a clean exit the transaction auto-commits; on an exception it
    auto-rolls-back and re-raises. ``commit()`` / ``rollback()`` may also be
    called explicitly on the yielded context for mid-sequence control.
    """
    policy = policy or Policy.allow_all()
    audit = audit or AuditJournal()

    for adapter in _unique(adapters):
        if hasattr(adapter, "begin"):
            adapter.begin()

    ctx = TxnContext(adapters, policy, audit)
    token = active_txn.set(ctx)
    try:
        yield ctx
    except Exception:
        if not ctx._finished:
            ctx.rollback()
        raise
    else:
        if not ctx._finished:
            ctx.commit()
    finally:
        active_txn.reset(token)
