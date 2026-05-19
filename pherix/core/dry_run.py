"""Speculative dry-run — fold forward against a snapshot, then discard.

Slice 7 is mechanically ``agent_txn`` that rolls back at the end instead
of committing. The same fold over the same journal, against the same
adapters, with the existing ``BEGIN`` / ``ROLLBACK`` bracket already
giving "discard the world" for free. The whole contribution is wiring:

- A top-level :func:`dry_run` context manager (D1) — its own register
  alongside :func:`agent_txn` / :func:`run_txn` / :func:`replay` so
  future dry-run knobs don't grow ``agent_txn``'s contract.
- A :class:`DryRunResult` carrying the journal, the ``would_have_fired``
  filter, and the captured policy verdicts (D3).
- Capture-mode policy: stage-time calls swap :meth:`Policy.evaluate` for
  :meth:`Policy.try_evaluate`; commit-time uses
  :meth:`Policy.collect_verdicts`. Neither raises on Deny — verdicts
  flow into the result instead (D4).

Deferred to Slice 7.5 — do not build ahead:

- **Per-adapter structured state diff.** "Which rows would have been
  inserted? Which files would have been written?" requires each adapter
  to opt into a ``StateDiffable`` sub-protocol — genuine new work.
  Slice 7's :class:`DryRunResult` carries no ``state_diff`` field; the
  journal *is* the structural record at this slice.
- **Concurrency-aware dry-run.** Participation in
  :data:`pherix.core.isolation.REGISTRY` with soft-claim semantics, plus
  cross-process arbitration. Slice 7 does not run the isolation conflict
  diff — a dry-run that rolls back has no commit to race anyone for.

Maths framing: a real transaction is a forward fold of the journal
ending in ``adapter.commit``. A dry-run is the same forward fold ending
in ``adapter.rollback`` — the *measurement without collapse*. What you
observe is the journal-as-built (plus the policy verdicts that would
fire); what survives is nothing.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from pherix.core.adapters.base import TransactionalResourceAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect
from pherix.core.isolation import REGISTRY as ISOLATION_REGISTRY
from pherix.core.policy import Policy, PolicyVerdict
from pherix.core.tools import active_txn


def _unique(adapters: dict[str, Any]) -> list[Any]:
    """Distinct adapter instances (one adapter may serve several resource keys).

    Duplicated from :mod:`pherix.core.runtime` rather than imported — the
    dry-run module would otherwise sit downstream of the runtime, but the
    runtime's ``_dry_run_finalise`` imports :class:`DryRunResult` from
    here. Keeping the helper local breaks the cycle without a third
    helpers module.
    """
    seen: set[int] = set()
    out: list[Any] = []
    for a in adapters.values():
        if id(a) not in seen:
            seen.add(id(a))
            out.append(a)
    return out


@dataclass
class DryRunResult:
    """The product of a dry-run — three observation layers, no side effects.

    :attr:`journal` is the per-effect record exactly as a real
    :func:`agent_txn` would have produced. Every :class:`Effect`
    carries its tool / args / resource / reversible flag / status /
    snapshot handle / read_keys / write_keys — the dry-run *did* fold
    forward, the rollback at the end is what makes it dry.

    :attr:`would_have_fired` is the slice of :attr:`journal` filtered by
    ``(reversible=False, status=STAGED)`` — the irreversibles that
    Slice 3's staging path *would* have fired at commit-time. Their
    ``apply`` functions never ran (the staged-fire loop never executed),
    so the agent's ``StagedResult`` sentinel is the only observation.

    :attr:`policy_verdicts` is the flat list of every
    :class:`PolicyVerdict` captured during the dry-run: stage-time
    verdicts (one per rule/cap per effect, in agent-body order) followed
    by commit-time verdicts (one per rule/cap per effect, in journal
    order — produced by :meth:`Policy.collect_verdicts`).
    :attr:`is_clean` is ``all(v.allow for v in policy_verdicts)`` —
    the conjunction of every verdict in the bundle.

    Layer 4 — per-adapter structured state diff ("rows added", "files
    written") — defers to Slice 7.5 via an opt-in ``StateDiffable``
    sub-protocol on adapters.
    """

    txn_id: str
    journal: list[Effect]
    would_have_fired: list[Effect]
    policy_verdicts: list[PolicyVerdict]
    is_clean: bool
    # Slice 8: per-resource structured state delta, keyed by adapter name.
    # Each value is the adapter's :meth:`StateDiffable.state_diff` output —
    # SQL: ``{"rows_added": [...], "rows_modified": [...],
    # "rows_deleted": [...]}``; FS: ``{"files_added": [...],
    # "files_modified": [...], "files_deleted": [...]}``. Empty for adapters
    # that do not opt into :class:`~pherix.core.adapters.base.StateDiffable`
    # (e.g. the irreversible HTTP adapter, whose structural record is
    # :attr:`would_have_fired`). Populated at the dry-run finalise hook,
    # *before* the rollback discards the world.
    state_diff: dict = field(default_factory=dict)


@contextmanager
def dry_run(
    adapters: dict[str, Any],
    *,
    policy: Policy | None = None,
    audit: AuditJournal | None = None,
    client_id: str | None = None,
) -> Iterator[Any]:
    """Speculative-execution context manager.

    Inside the ``with`` block, the agent's tool calls intercept exactly
    as they do under :func:`agent_txn` — same journalling, same
    snapshots, same ``StagedResult`` sentinels for irreversibles. On
    exit, the snapshot/rollback bracket discards the world and the
    fully-populated :class:`DryRunResult` lands on ``ctx.result``.

    Usage::

        from pherix import dry_run

        with dry_run({"sql": SQLiteAdapter(conn)}, policy=my_policy) as ctx:
            agent_step(...)
            agent_step(...)

        print(ctx.result.would_have_fired)
        print(ctx.result.policy_verdicts)
        assert ctx.result.is_clean

    Policy denial during the body does NOT abort the ``with`` block —
    the verdict is captured into the result instead, and the body keeps
    running so the full journal materialises. Genuine errors (adapter
    failures, malformed effects) still raise as in a normal txn; a
    raised body means there is no result to inspect.

    The world after the ``with`` block exits is bit-identical to its
    pre-call state, with two intentional exceptions: ``ctx.result`` is
    populated, and the audit row for this txn carries ``dry_run=1`` so
    operators can filter dry-runs out of compliance views.
    """
    # Local import to avoid the runtime ↔ dry_run cycle (runtime imports
    # DryRunResult; dry_run imports TxnContext).
    from pherix.core.runtime import TxnContext

    policy = policy or Policy.allow_all()
    audit = audit or AuditJournal.in_memory()

    for adapter in _unique(adapters):
        if isinstance(adapter, TransactionalResourceAdapter):
            adapter.begin()

    ctx = TxnContext(adapters, policy, audit, dry_run=True, client_id=client_id)
    # Slice 4 (D5): register the context with the in-process arbitration
    # substrate — same as :func:`agent_txn` — so :class:`Serialize`
    # waiters can see us as a peer. Dry-runs don't actually compete for
    # commit (we always rollback), but registering keeps the bookkeeping
    # honest; if Slice 7.5 adds concurrency-aware dry-run it slots in
    # without churning this scaffold.
    ISOLATION_REGISTRY.register(ctx)
    token = active_txn.set(ctx)
    try:
        try:
            yield ctx
            if not ctx._finished:
                ctx._dry_run_finalise()
        except Exception:
            # Genuine error in the body: unwind cleanly with the existing
            # rollback bracket. No result materialises — the caller sees
            # the exception, ctx.result stays None.
            if not ctx._finished:
                ctx.rollback()
            raise
        finally:
            active_txn.reset(token)
    finally:
        ISOLATION_REGISTRY.unregister(ctx)


__all__ = [
    "DryRunResult",
    "dry_run",
]
