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

from pherix.core.adapters.base import StateDiffable, TransactionalResourceAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus, StagedResult
from pherix.core.isolation import (
    REGISTRY as ISOLATION_REGISTRY,
    Abort,
    Serialize,
    _RetrySignal,
    check_conflicts,
)
from pherix.core.envelope import (
    flush_increments,
    is_durable_cap,
    pending_increments,
)
from pherix.core.policy import (
    Policy,
    PolicyContext,
    PolicyVerdict,
    PolicyViolation,
    sql_reader,
)
from pherix.core.tools import REGISTRY, active_actor, active_effect, active_txn
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


def _verdict_rows(verdicts: list[Any]) -> list[dict]:
    """Normalise :class:`~pherix.core.policy.PolicyVerdict` objects into the
    plain dicts :meth:`AuditJournal.record_verdicts` persists.

    ``kind`` is derived from the rule object: ``None`` is the allow/deny
    tool-name list, a cap (its class name carries ``Cap``) is ``'cap'``,
    everything else is a ``'rule'``. Keeps the audit layer free of any
    policy-type import — the runtime owns the translation.
    """
    rows: list[dict] = []
    for v in verdicts:
        rule = getattr(v, "rule", None)
        if rule is None:
            kind = "allowlist"
        elif "Cap" in type(rule).__name__:
            kind = "cap"
        else:
            kind = "rule"
        rows.append(
            {
                "effect_index": v.effect_index,
                "phase": v.where,
                "allow": v.allow,
                "kind": kind,
                "rule_name": v.rule_name,
                "reason": v.reason,
            }
        )
    return rows


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
        *,
        dry_run: bool = False,
        client_id: str | None = None,
        actor: str | None = None,
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
        # Slice 6: a single PolicyContext per txn carries the journal-so-far
        # reference + per-cap running totals across every stage-time
        # evaluate() call. The same ctx is reused for the commit-time
        # evaluate_journal walk (which resets caps and re-folds).
        # #7 (engine-hardening): thread a read mediator over the adapter map
        # into the context so world-state-aware rules can call
        # ``ctx.read(resource, key)``. The mediator is a closure over
        # ``adapters`` — cheap to build, issues no query until a rule actually
        # reads. The same ``_policy_ctx`` is reused across the stage-time and
        # commit-time walks, so a rule that reads live state at both moments
        # sees the world as it stood at each — that divergence is the TOCTOU
        # protection the twice-evaluated bracket exists for.
        self._policy_ctx = PolicyContext(
            journal=self.txn.effects,
            where="stage",
            reader=sql_reader(adapters),
        )
        # Slice 7: dry-run mode flips policy evaluation from raise-mode
        # (``evaluate``) to capture-mode (``try_evaluate``) at stage-time,
        # so a Deny during the body lands in ``_stage_verdicts`` instead of
        # aborting the with-block. The audit row carries ``dry_run=1`` so
        # operators can filter dry-runs out of compliance views.
        self._dry_run = dry_run
        # Slice 8: provenance for a gateway front-end serving many MCP
        # clients through one core. Threaded as a keyword-only param exactly
        # as ``dry_run`` is; written into the nullable ``client_id`` audit
        # column. Library callers never supply one and the column stays NULL.
        self._client_id = client_id
        # Actor: the transaction-level *default* principal each effect inherits
        # (the "on whose authority" provenance), distinct from ``client_id``
        # (which agent/session produced the effect). Held here as the txn
        # default; the live per-effect value is read from the ``active_actor``
        # contextvar at stamp-time, so :func:`pherix.core.tools.acting_as` can
        # override it per call (one agent acting for several principals). This
        # is attribution, not auth — Pherix records the claimed actor, never
        # verifies it.
        self._actor = actor
        self._stage_verdicts: list[PolicyVerdict] = []
        # Slice 8: capture a read-only state baseline per StateDiffable
        # adapter at txn begin. A SQLite SAVEPOINT is not separately
        # queryable as a before-image, so the structural diff cannot read the
        # pre-image from inside the per-effect snapshot lane — instead it
        # diffs the live resource at finalise against this parallel baseline.
        # Only the dry-run finalise consumes it, so the capture is gated on
        # ``dry_run``: a committed agent_txn never pays the full-table-dump
        # cost on its hot path. Captured eagerly (the context managers call
        # adapter.begin() before constructing the ctx, so the resource is
        # already inside its txn bracket and the baseline is the pre-effect
        # state). Keyed by id(adapter) to dedupe one adapter serving several
        # resource keys. Empty unless an adapter opts into StateDiffable.
        self._state_baselines: dict[int, tuple[Any, Any]] = (
            {
                id(a): (a, a.state_baseline())
                for a in _unique(adapters)
                if isinstance(a, StateDiffable)
            }
            if dry_run
            else {}
        )
        # Populated by :meth:`_dry_run_finalise` once the body completes.
        self.result: Any = None
        audit.record_transaction(self.txn, dry_run=dry_run, client_id=client_id)

    @property
    def txn_id(self) -> str:
        return self.txn.txn_id

    # --- interception ---

    def record_tool_call(self, tool_name: str, args: tuple, kwargs: dict) -> Any:
        self._guard_thread()
        self._guard_open()
        spec = REGISTRY.get(tool_name)

        # Stage-time compensator-name validation (D2). Catches typos before
        # any state changes; the journal stores the resolved name.
        if spec.compensator is not None and spec.compensator not in REGISTRY:
            raise CompensatorNotRegistered(spec.compensator, tool_name)

        adapter = self._resolve_adapter(spec.resource)
        # Stamp the actor (the principal this effect runs on behalf of) from
        # the live context: the ``active_actor`` contextvar, which the runtime
        # seeded with this txn's default at ``agent_txn(.., actor=)`` time and
        # which ``acting_as`` may have overridden for this specific call.
        # Falls back to the txn default if the contextvar is unset (e.g. an
        # effect journalled outside the ``agent_txn`` seeding path).
        effect_actor = active_actor.get()
        if effect_actor is None:
            effect_actor = self._actor
        effect = Effect(
            txn_id=self.txn.txn_id,
            index=self.txn.next_index(),
            tool=tool_name,
            args=spec.bind_args(args, kwargs),
            resource=spec.resource,
            reversible=adapter.supports_rollback(),
            compensator=spec.compensator,
            actor=effect_actor,
        )

        # Slice 6: stage-time policy evaluation against the fully-built
        # Effect. Rules and caps need ``effect.args`` and the journal-so-far
        # (held live by ``self._policy_ctx``) — neither is available from
        # the tool name alone. On Deny nothing is journalled, no resource is
        # touched, no audit row written. The Effect object is discarded
        # (effect_id is deterministic — a re-attempt rebuilds an identical id).
        #
        # Slice 7: in dry-run mode the stage-time path captures verdicts
        # without raising, so the agent body keeps running and the full
        # journal materialises for the final ``DryRunResult``.
        if self._dry_run:
            self._stage_verdicts.extend(
                self._policy.try_evaluate(
                    effect, self._policy_ctx, where="stage"
                )
            )
        else:
            self._policy.evaluate(effect, self._policy_ctx, where="stage")

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

        # Slice 6: commit-time policy bracket. Re-walk the journal and
        # re-evaluate every applicable rule against every effect (D3 timing).
        # For Slice 6's args-only rules the verdicts match stage-time
        # exactly; the bracket lands as architecture so Slice 6.5's
        # world-state-aware rules slot in without engine surgery. Walks the
        # entire journal — reversibles included — because rules can deny on
        # any effect's args, not only the staged irreversibles. Replaces the
        # Slice 3 per-staged ``policy.check(e.tool)`` loop.
        try:
            self._policy.evaluate_journal(self.txn, self._policy_ctx)
        except PolicyViolation as exc:
            if (
                exc.effect_index is not None
                and 0 <= exc.effect_index < len(self.txn.effects)
            ):
                denied = self.txn.effects[exc.effect_index]
                denied.status = EffectStatus.GATED
                self.audit.update_effect(denied)
            # Unwind path depends on whether we've already transitioned to
            # STAGED (irreversibles present): partial_unwind handles the
            # mixed-fold case via the PARTIAL state; otherwise the txn is
            # still OPEN and a plain rollback restores reversibles. At this
            # point no staged irreversible has fired yet — irreversibles
            # remain STAGED, the strongest containment property.
            if self.txn.state is TxnState.STAGED:
                self._partial_unwind()
            else:
                self.rollback()
            raise

        if staged:
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
        # #10 (engine-hardening): consume the longitudinal budget. Durable
        # caps fold their per-txn contribution into the cross-run total ONLY
        # here, on the successful-commit path — never in rollback,
        # _partial_unwind, _dry_run_finalise, or the gate/policy unwind
        # branches. A rolled-back, gated, or denied txn must consume no
        # budget. Each EnvelopeIncrement carries its own store, so a policy
        # mixing caps bound to different stores flushes correctly.
        durable = [c for c in self._policy.caps if is_durable_cap(c)]
        if durable:
            flush_increments(pending_increments(durable, self.txn.effects))
        self._finished = True

    # --- Slice 7: dry-run finalise ----------------------------------------

    def _dry_run_finalise(self) -> None:
        """Commit-time bracket for a dry-run: capture verdicts, build the
        result, then unwind everything via the existing rollback bracket.

        Three steps, in this exact order:

          1. ``Policy.collect_verdicts`` re-walks the journal in capture
             mode (no short-circuit, no raise). The full stage-time +
             commit-time verdict list is what populates the result.
          2. Build the :class:`DryRunResult` from the live journal, the
             ``would_have_fired`` filter, and the verdict list.
          3. ``rollback()`` runs the existing snapshot-restore + adapter
             rollback bracket, taking the txn ``OPEN → ROLLED_BACK``.

        The world is bit-identical to its pre-dry-run state on exit
        except for the populated ``self.result`` and the audit row (which
        carries ``dry_run=1`` so compliance views can filter it out).
        """
        # Avoid the cycle: import here so :mod:`pherix.core.runtime` does
        # not depend on :mod:`pherix.core.dry_run` at import time.
        from pherix.core.dry_run import DryRunResult

        commit_verdicts = self._policy.collect_verdicts(
            self.txn, self._policy_ctx
        )
        all_verdicts = list(self._stage_verdicts) + list(commit_verdicts)
        would_have_fired = [
            e
            for e in self.txn.effects
            if (not e.reversible) and e.status is EffectStatus.STAGED
        ]
        self.result = DryRunResult(
            txn_id=self.txn.txn_id,
            journal=list(self.txn.effects),
            would_have_fired=would_have_fired,
            policy_verdicts=all_verdicts,
            is_clean=all(v.allow for v in all_verdicts),
            state_diff=self._compute_state_diff(),
        )

        # Persist the captured verdicts so the inspector can render the
        # per-rule stage/commit decisions (incl. any world-state divergence).
        # Best-effort: the verdict record annotates the journal; a failure to
        # write it must never change the dry-run's outcome (the effect
        # statuses are the source of truth). Append-only, like everything else.
        try:
            self.audit.record_verdicts(
                self.txn.txn_id, _verdict_rows(all_verdicts)
            )
        except Exception:
            pass

        # Unwind: identical mechanics to a normal rollback. Reversibles
        # restore from snapshots (APPLIED → COMPENSATED); irreversibles
        # were never fired (STAGED) and have no snapshot to restore.
        self.rollback()

    def _compute_state_diff(self) -> dict[str, dict]:
        """Per-resource structural delta — current state vs the begin baseline.

        Called from :meth:`_dry_run_finalise` *before* the rollback, so the
        live resource still carries the dry-run's writes. For every
        :class:`StateDiffable` adapter whose baseline was captured at
        ``__init__``, dispatch ``adapter.state_diff(baseline)`` and key the
        result by ``adapter.name`` — yielding the cross-driver contract shape
        ``{"sql": {...}, "fs": {...}}``. Adapters that did not opt in
        contribute nothing (their baseline was never captured), so the HTTP
        adapter is silently absent — its structural record is
        ``would_have_fired``.
        """
        out: dict[str, dict] = {}
        for adapter, baseline in self._state_baselines.values():
            out[adapter.name] = adapter.state_diff(baseline)
        return out

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
        # Prong #2: persist the conflict as a first-class journal record
        # BEFORE handing to the resolution policy. The diff fires only at
        # commit, after the body's effects are all journalled, so the txn_id
        # row already exists — no orphan. Writing here (not inside resolve)
        # means the record survives both Abort/Serialize (resolve raises,
        # the conflict is already durable) AND Retry (resolve raises the
        # internal _RetrySignal, the txn rolls back and the body replays — so
        # every attempt that conflicts leaves its own record, and the journal
        # can finally count what it used to swallow). Best-effort and
        # append-only like the verdict record: a write failure must never
        # change the txn's fate, which the resolution policy decides next.
        try:
            self.audit.record_conflicts(self.txn.txn_id, conflicts)
        except Exception:
            pass
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
    *,
    client_id: str | None = None,
    actor: str | None = None,
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

    ``actor`` is the transaction-level *default* principal — the party every
    effect in the block runs *on behalf of* (e.g. ``"alice"``,
    ``"role:admin"``). It is stamped onto each :class:`Effect` as it is
    journalled, and persisted in the audit journal. A per-call override is
    available via :func:`pherix.core.tools.acting_as`, for the case where one
    agent session acts for several principals across calls. ``actor`` is
    *attribution, not identity*: Pherix records the claimed principal but does
    NOT verify it, and it is distinct from ``client_id`` (which agent/session
    produced the effect). Library callers who never set it leave the column
    NULL.
    """
    policy = policy or Policy.allow_all()
    audit = audit or AuditJournal.default()

    for adapter in _unique(adapters):
        if isinstance(adapter, TransactionalResourceAdapter):
            adapter.begin()

    ctx = TxnContext(
        adapters,
        policy,
        audit,
        isolation=isolation,
        client_id=client_id,
        actor=actor,
    )
    # Slice 4 (D5): register the open ctx with the in-process arbitration
    # substrate so a concurrent Serialize commit can find us and wait.
    ISOLATION_REGISTRY.register(ctx)
    token = active_txn.set(ctx)
    # Seed the per-effect actor contextvar with this txn's default. Each Effect
    # reads ``active_actor`` as it is journalled; ``acting_as`` rebinds it for
    # per-call overrides. Reset on every exit path alongside ``active_txn``.
    actor_token = active_actor.set(actor)
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
            active_actor.reset(actor_token)
    finally:
        # Unregister AFTER active_txn reset and AFTER rollback/commit have
        # run — so the close-event fires only once the txn is truly done
        # and no Serialize waiter wakes up on a still-in-flight state.
        ISOLATION_REGISTRY.unregister(ctx)
