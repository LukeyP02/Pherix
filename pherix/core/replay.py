"""Slice 5 — replay the journal forward against fresh state.

The journal is a time series of effects; replay is a forward fold of that
series. ``verify`` mode asserts each replayed effect's result equals the
source's recorded result (under a per-tool comparator); ``reconstruct``
mode accepts whatever today's apply produces and rebuilds the world the
journal described on the operator-supplied fresh substrate.

Both modes share the same walker. The mode parameter selects two things:

- per-effect: how to interpret the replayed result (assert-equal vs
  accept-as-new-state);
- per-tool: irreversible effects with ``status='APPLIED'`` in the source
  journal are *never* re-fired — Pherix cannot honestly snapshot them, so
  the journal *is* the witness. The recorded result is reused on the
  replay txn under both modes (Slice 3's ``effect_id`` idempotency
  earning its keep at scale — retires the Slice-3 follow-up that flagged
  the idempotency test as "a pin, not a scenario").
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pherix.core.adapters.base import TransactionalResourceAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus, strict_json_default
from pherix.core.isolation import check_conflicts
from pherix.core.policy import Policy
from pherix.core.tools import REGISTRY, active_effect
from pherix.core.transaction import Transaction, TxnState

# Sentinel the audit emits for bytes (see ``strict_json_default``). On the
# read path we recognise it and decode back to ``bytes`` so tool args /
# results that originally carried binary data round-trip cleanly.
_BYTES_PREFIX = "<bytes:b64:"
_BYTES_SUFFIX = ">"


def _decode_bytes(value: Any) -> Any:
    """Walk a JSON-deserialised tree and decode the bytes sentinel back to bytes.

    The audit journal stores bytes as ``<bytes:b64:...>`` strings (see
    :func:`strict_json_default`). On replay we want the tool to see the same
    Python object it saw originally, not a sentinel string, so the FS demo's
    ``body: bytes`` argument really arrives as bytes. Datetime and dataclass
    payloads stay as ISO strings / nested dicts respectively — recovering
    those would need type metadata we don't carry, and the slice's contract
    doesn't promise it.
    """
    if isinstance(value, str):
        if value.startswith(_BYTES_PREFIX) and value.endswith(_BYTES_SUFFIX):
            payload = value[len(_BYTES_PREFIX):-len(_BYTES_SUFFIX)]
            return base64.b64decode(payload)
        return value
    if isinstance(value, list):
        return [_decode_bytes(v) for v in value]
    if isinstance(value, dict):
        return {k: _decode_bytes(v) for k, v in value.items()}
    return value


def _canonical_json(value: Any) -> str:
    """Canonical sorted-JSON encoding — the default comparator's normal form."""
    return json.dumps(value, default=strict_json_default, sort_keys=True)


def _default_comparator(recorded: Any, replayed: Any) -> bool:
    """JSON-string equality through ``strict_json_default``.

    ``recorded`` is the source effect's ``result`` as it sat in the audit
    row: a Python value loaded back from the row's stored JSON. ``replayed``
    is whatever today's tool returned. Pushing both through
    :func:`_canonical_json` puts them on a level field — bytes/datetime/
    dataclass values from today get rendered the same way the audit row
    rendered yesterday — and then string-equality decides the verdict.
    """
    return _canonical_json(recorded) == _canonical_json(replayed)


@dataclass
class EffectOutcome:
    """One row of :class:`ReplayResult.outcomes` — what happened on replay."""

    index: int
    tool: str
    resource: str
    # One of: "match", "divergence", "applied",
    # "skipped_idempotent", "skipped_non_applied".
    status: str
    recorded_result: Any = None
    replayed_result: Any = None
    message: str | None = None


@dataclass
class ReplayResult:
    """Outcome of one :func:`replay` call."""

    mode: str
    source_txn_id: str
    replay_txn_id: str
    # "success" if every effect matched (verify) or replayed cleanly
    # (reconstruct); "divergence" if any verify-mode comparator returned
    # False; "failure" if replay raised mid-walk.
    status: str
    outcomes: list[EffectOutcome] = field(default_factory=list)
    divergences: list[EffectOutcome] = field(default_factory=list)
    # Mirrors ``check_conflicts(replay_txn.effects, adapters)`` at commit-time.
    # Empty list under a clean replay; a non-empty list flags Slice-4 contract
    # leakage and is treated as a divergence under verify mode.
    isolation_conflicts: list = field(default_factory=list)


class ReplayDivergence(RuntimeError):
    """Raised under verify mode when ``raise_on_divergence=True`` (the default).

    Carries the :class:`ReplayResult` so the caller can inspect per-effect
    outcomes without re-parsing the message.
    """

    def __init__(self, result: ReplayResult):
        self.result = result
        names = ", ".join(
            f"[{o.index}] {o.tool}" for o in result.divergences[:5]
        )
        more = "" if len(result.divergences) <= 5 else f" (+{len(result.divergences) - 5} more)"
        super().__init__(
            f"replay of {result.source_txn_id} diverged on "
            f"{len(result.divergences)} effect(s): {names}{more}"
        )


def _load_source_effect(row: dict) -> dict:
    """Decode an audit row into a Python dict the walker can dispatch on.

    The audit stores ``args``, ``snapshot``, ``result``, ``read_keys``,
    ``write_keys`` as JSON strings. Replay needs them as Python values.
    Bytes sentinels are decoded back to ``bytes`` so the tool call sees
    the same object the original call did.
    """
    return {
        "idx": int(row["idx"]),
        "tool": row["tool"],
        "resource": row["resource"],
        "reversible": bool(row["reversible"]),
        "status": row["status"],
        "effect_id": row["effect_id"],
        "args": _decode_bytes(json.loads(row["args"])),
        "result": _decode_bytes(json.loads(row["result"])) if row["result"] is not None else None,
        "read_keys": json.loads(row["read_keys"] or "[]"),
        "write_keys": json.loads(row["write_keys"] or "[]"),
    }


def _compare(spec_comparator: Callable[[Any, Any], bool] | None, recorded: Any, replayed: Any) -> bool:
    """Pick the comparator: per-tool override if registered, default otherwise."""
    fn = spec_comparator or _default_comparator
    return fn(recorded, replayed)


def replay(
    txn_id: str,
    adapters: dict[str, Any],
    *,
    source_audit: AuditJournal,
    target_audit: AuditJournal | None = None,
    mode: Literal["verify", "reconstruct"] = "verify",
    policy: Policy | None = None,
    raise_on_divergence: bool = True,
) -> ReplayResult:
    """Re-fire the journal of ``txn_id`` against ``adapters`` (D1, D5).

    ``source_audit`` is the audit journal holding the transaction being
    replayed (typically loaded from an on-disk path). ``target_audit`` is
    where the replay's own journal lands — every replayed effect produces
    a row there, and the replay txn carries ``replayed_from=txn_id`` so an
    operator can ask of any txn "are you anyone's replay-target?" via a
    single field lookup. Defaults to an in-memory journal for verify mode,
    where the replay's own audit is usually disposable.

    ``adapters`` are operator-supplied — Pherix never constructs fresh
    adapters for the operator (DB paths, FS roots, HTTP creds are
    environment specifics Pherix has no business knowing). The operator
    opens whatever fresh resource state they want; Pherix walks the
    journal against it.

    Under ``mode='verify'`` (the default): each effect's recorded result
    is compared to the replayed result via the registered tool's
    :attr:`ToolSpec.comparator` (or the default JSON-string equality).
    Mismatches land in :attr:`ReplayResult.divergences`. With
    ``raise_on_divergence=True`` (default), any divergence raises
    :class:`ReplayDivergence` carrying the full :class:`ReplayResult`.

    Under ``mode='reconstruct'``: replayed results are accepted as the
    new world's state and no comparison is performed. Irreversible
    effects with ``status='APPLIED'`` in the source journal are *never*
    re-fired under either mode (D3) — they are reused from the journal
    because Pherix cannot honestly snapshot them, and refiring them
    would double-bill the external world.
    """
    if mode not in ("verify", "reconstruct"):
        raise ValueError(f"mode must be 'verify' or 'reconstruct'; got {mode!r}")
    target_audit = target_audit if target_audit is not None else AuditJournal.in_memory()
    policy = policy if policy is not None else Policy.allow_all()

    source_rows = source_audit.get_effects(txn_id)
    if not source_rows:
        raise ValueError(
            f"no effects found in source_audit for txn_id {txn_id!r}; nothing to replay"
        )
    source_effects = [_load_source_effect(r) for r in source_rows]

    # Open the replay transaction. ``replayed_from`` is the audit-level
    # link to the source. State machine is the existing OPEN -> COMMITTED /
    # ROLLED_BACK; replay reuses Slice 1 mechanics rather than inventing
    # parallel state.
    replay_txn = Transaction(policy=policy, replayed_from=txn_id)
    target_audit.record_transaction(replay_txn)

    # Lifecycle on transactional adapters. The operator handed us fresh
    # adapters; we drive their per-txn bracket the same way agent_txn does.
    bracket_adapters = _unique_adapters(adapters)
    for adapter in bracket_adapters:
        if isinstance(adapter, TransactionalResourceAdapter):
            adapter.begin()

    result = ReplayResult(
        mode=mode,
        source_txn_id=txn_id,
        replay_txn_id=replay_txn.txn_id,
        status="success",
    )

    try:
        _walk(
            source_effects=source_effects,
            adapters=adapters,
            replay_txn=replay_txn,
            target_audit=target_audit,
            mode=mode,
            policy=policy,
            result=result,
        )

        # Commit-time sanity diff: a journal whose original commit cleared
        # check_conflicts should clear it on replay too. A non-empty list
        # is a Slice 4 contract leak and counts as divergence under verify.
        conflicts = check_conflicts(replay_txn.effects, adapters)
        result.isolation_conflicts = conflicts
        if conflicts and mode == "verify":
            result.status = "divergence"
            result.divergences.append(
                EffectOutcome(
                    index=-1,
                    tool="<isolation>",
                    resource="<commit>",
                    status="divergence",
                    message=f"commit-time isolation conflict(s): {conflicts}",
                )
            )

        if result.divergences:
            result.status = "divergence"
            _finalise(bracket_adapters, replay_txn, target_audit, committed=False)
        else:
            _finalise(bracket_adapters, replay_txn, target_audit, committed=True)
    except Exception:
        result.status = "failure"
        _finalise(bracket_adapters, replay_txn, target_audit, committed=False)
        raise

    if mode == "verify" and result.divergences and raise_on_divergence:
        raise ReplayDivergence(result)
    return result


def _walk(
    *,
    source_effects: list[dict],
    adapters: dict[str, Any],
    replay_txn: Transaction,
    target_audit: AuditJournal,
    mode: str,
    policy: Policy,
    result: ReplayResult,
) -> None:
    """The forward fold itself — one pass over the source journal."""
    for src in source_effects:
        adapter = adapters.get(src["resource"])
        if adapter is None:
            raise RuntimeError(
                f"replay: no adapter provided for resource {src['resource']!r} "
                f"(source effect [{src['idx']}] {src['tool']!r})"
            )

        if not src["reversible"]:
            # Irreversible lane. Either the source effect actually fired
            # (status APPLIED) — skip-and-reuse, the journal is the witness;
            # or it didn't (STAGED / GATED / FAILED / COMPENSATED) — also
            # skip, because re-firing what was never approved on the
            # original txn would invent state the operator never sanctioned.
            outcome = _handle_irreversible(
                src=src,
                replay_txn=replay_txn,
                target_audit=target_audit,
            )
            result.outcomes.append(outcome)
            continue

        # Reversible lane. Policy re-evaluation matches the runtime's D6
        # stage-time check; replay honours it for symmetry. A denied tool
        # raises PolicyViolation, which the outer try-except converts to
        # ReplayResult.status='failure'.
        policy.check(src["tool"])

        spec = REGISTRY.get(src["tool"])

        new_effect = Effect(
            txn_id=replay_txn.txn_id,
            index=replay_txn.next_index(),
            tool=src["tool"],
            args=src["args"],
            resource=src["resource"],
            reversible=True,
            compensator=spec.compensator,
        )
        replay_txn.add_effect(new_effect)
        target_audit.record_effect(new_effect)

        new_effect.snapshot = adapter.snapshot(new_effect)
        token = active_effect.set(new_effect)
        try:
            new_effect.result = adapter.apply(new_effect, spec.fn)
        except Exception:
            new_effect.status = EffectStatus.FAILED
            target_audit.update_effect(new_effect)
            raise
        finally:
            active_effect.reset(token)
        new_effect.status = EffectStatus.APPLIED
        target_audit.update_effect(new_effect)

        if mode == "verify":
            matched = _compare(spec.comparator, src["result"], new_effect.result)
            outcome = EffectOutcome(
                index=new_effect.index,
                tool=new_effect.tool,
                resource=new_effect.resource,
                status="match" if matched else "divergence",
                recorded_result=src["result"],
                replayed_result=new_effect.result,
            )
            if not matched:
                result.divergences.append(outcome)
            result.outcomes.append(outcome)
        else:
            # Reconstruct: today's result is the new world's state.
            result.outcomes.append(
                EffectOutcome(
                    index=new_effect.index,
                    tool=new_effect.tool,
                    resource=new_effect.resource,
                    status="applied",
                    recorded_result=src["result"],
                    replayed_result=new_effect.result,
                )
            )


def _handle_irreversible(
    *,
    src: dict,
    replay_txn: Transaction,
    target_audit: AuditJournal,
) -> EffectOutcome:
    """Irreversible effects: skip-and-reuse (D3) — never re-fire on replay.

    A registered effect on the replay txn still gets recorded so the
    target_audit row carries the full story (replayed_from, same tool,
    same result), but ``adapter.apply`` is not called. This is the
    moment ``effect_id`` idempotency does real work: at scale we are
    deciding "did this irreversible already fire under the source txn?"
    from the audit row's status alone.
    """
    new_effect = Effect(
        txn_id=replay_txn.txn_id,
        index=replay_txn.next_index(),
        tool=src["tool"],
        args=src["args"],
        resource=src["resource"],
        reversible=False,
    )
    replay_txn.add_effect(new_effect)

    if src["status"] == EffectStatus.APPLIED.name:
        new_effect.result = src["result"]
        new_effect.status = EffectStatus.APPLIED
        target_audit.record_effect(new_effect)
        return EffectOutcome(
            index=new_effect.index,
            tool=new_effect.tool,
            resource=new_effect.resource,
            status="skipped_idempotent",
            recorded_result=src["result"],
            replayed_result=src["result"],
            message="irreversible APPLIED in source — reused from journal, not re-fired",
        )

    # The source effect never actually fired (STAGED / GATED / FAILED /
    # COMPENSATED). Replay records the placeholder so the row count
    # matches, but the world is not asked to perform something the source
    # never asked it to perform either.
    new_effect.status = EffectStatus[src["status"]]
    target_audit.record_effect(new_effect)
    return EffectOutcome(
        index=new_effect.index,
        tool=new_effect.tool,
        resource=new_effect.resource,
        status="skipped_non_applied",
        recorded_result=src["result"],
        replayed_result=None,
        message=f"irreversible source status was {src['status']} — never fired, replay skips",
    )


def _finalise(
    bracket_adapters: list[Any],
    replay_txn: Transaction,
    target_audit: AuditJournal,
    *,
    committed: bool,
) -> None:
    """Close out lifecycle and persist terminal state."""
    for adapter in bracket_adapters:
        if isinstance(adapter, TransactionalResourceAdapter):
            if committed:
                adapter.commit()
            else:
                adapter.rollback()
    target_state = TxnState.COMMITTED if committed else TxnState.ROLLED_BACK
    replay_txn.transition(target_state)
    target_audit.update_transaction_state(replay_txn.txn_id, target_state.name)


def _unique_adapters(adapters: dict[str, Any]) -> list[Any]:
    """Distinct adapter instances — one adapter may serve several resource keys."""
    seen: set[int] = set()
    out: list[Any] = []
    for a in adapters.values():
        if id(a) not in seen:
            seen.add(id(a))
            out.append(a)
    return out
