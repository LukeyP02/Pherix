"""Crash-consistent recovery — resume an interrupted backward fold (#9).

The honest scope
================

``runtime._partial_unwind`` already folds the journal backward when a commit
fails *inside one live process*. The gap this module closes is the next failure
along the timeline: the process **dies** part-way through that unwind (or before
it ever started), leaving a transaction in a non-terminal durable state with
real-world side effects still standing.

The mental model is unchanged from the rest of Pherix — **everything is a
traversal of the journal**. Recovery is just the backward fold again, but driven
from the *durable* journal rather than the in-memory ``Transaction``, because the
in-memory object died with the process. We re-read the persisted effect rows,
re-derive what still needs undoing, and resume the fold to a terminal state.

What genuinely survives a crash — and what does not
====================================================

Be precise about which guarantees are real, because the value of Pherix is being
honest about exactly this:

- **Uncommitted reversible (SQL) writes** do *not* need recovery. A SQLite
  ``SAVEPOINT`` is connection-local; it dies with the process. But so does the
  enclosing ``BEGIN`` — SQLite auto-rolls-back any uncommitted transaction the
  instant the connection closes (process death closes it). So a reversible effect
  that was APPLIED-but-not-committed is *already undone by the database* before
  recovery ever runs. There is nothing in the live world to restore: the
  savepoint handle in the journal points at state that no longer exists. Recovery
  records this honestly (marks the effect COMPENSATED, "the DB already did it")
  and moves on. It does **not** try to ``ROLLBACK TO SAVEPOINT`` against a fresh
  connection — that savepoint name is meaningless cross-process.

- **APPLIED irreversible effects** are the real recovery target. An irreversible
  effect (charge a card, send an email, fire a webhook) left the process and
  changed the outside world. That side effect *persisted* across the crash, and
  so did the journal row recording it. The compensator — the effect's semantic
  left-inverse — is the only thing that can undo it, and the journal carries the
  effect's original args plus the compensator's name. Recovery re-fires it.

Exactly-once
============

The durable effect ``status`` is the idempotency fence. An irreversible effect is
compensated **iff** its durable row reads APPLIED. The moment its compensator
fires successfully, the row is flipped to COMPENSATED *and committed* to the
durable journal. A second recovery pass (or a crash mid-recovery followed by a
third) sees COMPENSATED and skips it — the compensator never runs twice. The
``effect_id`` is the stable name of the side effect; the status is whether its
inverse has been applied. Together they are the fence.

Terminal landing
================

After resuming the fold, the transaction lands terminal exactly as
``_partial_unwind`` would have:

- every APPLIED effect successfully undone (compensated, or DB-auto-rolled-back)
  → ``ROLLED_BACK``;
- any irreversible whose compensator is missing or itself raised → ``STUCK``
  (the journal still describes the standing artefact for manual recovery).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.tools import ToolRegistry
from pherix.core.tools import REGISTRY as _DEFAULT_REGISTRY
from pherix.core.transaction import TxnState

# A transaction is "mid-flight" — a candidate for recovery — when its durable
# state is non-terminal AND it owns at least one APPLIED effect. The applied
# effect is the proof that real work happened that the unwind has not yet
# reversed. A non-terminal txn with no applied effects (e.g. OPEN with only
# STAGED irreversibles) has nothing standing in the world — nothing to recover.
_RECOVERABLE_STATES: frozenset[str] = frozenset(
    {TxnState.OPEN.name, TxnState.STAGED.name, TxnState.PARTIAL.name, TxnState.STUCK.name}
)


@dataclass
class EffectRecovery:
    """What recovery did to one effect during the resumed fold."""

    effect_id: str
    index: int
    tool: str
    reversible: bool
    # One of: "compensated" (irreversible inverse fired now),
    # "db_auto_rolled_back" (reversible — SQLite undid it on process death),
    # "already_compensated" (durable status was already COMPENSATED — the
    # idempotency fence skipped it), "stuck_missing_compensator",
    # "stuck_compensator_raised".
    action: str
    error: str | None = None

    @property
    def is_stuck(self) -> bool:
        return self.action.startswith("stuck_")


@dataclass
class TxnRecovery:
    """Outcome of resuming the backward fold for one transaction."""

    txn_id: str
    prior_state: str
    final_state: str
    effects: list[EffectRecovery]

    @property
    def compensators_fired(self) -> int:
        return sum(1 for e in self.effects if e.action == "compensated")


@dataclass
class RecoveryReport:
    """Aggregate outcome of a recovery sweep over a durable journal."""

    transactions: list[TxnRecovery]

    @property
    def recovered(self) -> int:
        return sum(1 for t in self.transactions if t.final_state == TxnState.ROLLED_BACK.name)

    @property
    def stuck(self) -> int:
        return sum(1 for t in self.transactions if t.final_state == TxnState.STUCK.name)

    @property
    def compensators_fired(self) -> int:
        return sum(t.compensators_fired for t in self.transactions)


def _open_db(journal: AuditJournal | str) -> tuple[sqlite3.Connection, AuditJournal | None]:
    """Resolve the journal argument to a live connection.

    Accepts either an :class:`AuditJournal` (reuse its connection — same
    process, same DB) or a path string (open our own connection to the durable
    file, the cross-process / "new process after a crash" case). Returns the
    connection plus the AuditJournal if one was supplied (so we never close a
    connection the caller still owns).
    """
    if isinstance(journal, AuditJournal):
        # Reuse the caller's connection. row_factory is already sqlite3.Row.
        return journal._conn, journal
    conn = sqlite3.connect(journal)
    conn.row_factory = sqlite3.Row
    return conn, None


def _set_effect_status(conn: sqlite3.Connection, txn_id: str, idx: int, status: str) -> None:
    """Flip one durable effect row's status and commit it immediately.

    Committing per-effect (not per-transaction) is what makes the fence
    crash-safe: a crash *during* recovery leaves every already-undone effect
    durably COMPENSATED, so the next pass skips it. Parameterised SQL only.
    """
    conn.execute(
        "UPDATE effects SET status = ? WHERE txn_id = ? AND idx = ?",
        (status, txn_id, idx),
    )
    conn.commit()


def _set_txn_state(conn: sqlite3.Connection, txn_id: str, state: str) -> None:
    conn.execute(
        "UPDATE transactions SET state = ?, updated_at = updated_at WHERE txn_id = ?",
        (state, txn_id),
    )
    conn.commit()


class CorruptJournalError(ValueError):
    """The durable journal is corrupted past safe interpretation — a row is
    missing columns, carries an unknown status, or has non-JSON args.

    Recovery fails **loud** with this (a ``ValueError`` subclass) rather than
    crashing with a cryptic ``IndexError``: a corrupt journal must be an honest,
    investigate-now signal, never a silently-wrong recovery. Found by the
    byteflip fuzz suite (``tests/test_fuzz_journal.py``).
    """


def _effect_from_row(row: sqlite3.Row) -> Effect:
    """Rehydrate an :class:`Effect` from a durable journal row.

    Only the fields the backward fold needs are reconstructed: tool, args,
    resource, reversibility, status, index. The compensator name is *not* read
    from the row — the durable ``effects`` table does not persist it (it is a
    property of the tool, resolved from the registry at fire-time, the same way
    the live unwind resolves it). The snapshot is *not* rehydrated into a live
    handle either — a SAVEPOINT does not survive the crash, so there is nothing
    for it to restore against (see the module docstring).

    A corrupted/truncated journal can present a row **missing columns** —
    ``sqlite3.Row`` raises a cryptic ``IndexError`` on the absent key — which we
    surface as a typed :class:`CorruptJournalError` (loud, not a crash). An
    unknown status (``KeyError``) or non-JSON args (``JSONDecodeError`` ⊂
    ``ValueError``) are *already* loud, typed failures, so they propagate as-is
    (callers / fuzz oracles assert those exact types).
    """
    try:
        return Effect(
            txn_id=row["txn_id"],
            index=row["idx"],
            tool=row["tool"],
            args=json.loads(row["args"]),
            resource=row["resource"],
            reversible=bool(row["reversible"]),
            effect_id=row["effect_id"],
            status=EffectStatus[row["status"]],
        )
    except IndexError as exc:
        raise CorruptJournalError(
            f"corrupt journal effect row (missing column): {exc}"
        ) from exc


def _find_mid_flight(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Durable transactions left mid-flight: non-terminal state with an APPLIED
    effect. The APPLIED effect is the evidence that real work is standing in the
    world that the backward fold has not yet reversed."""
    placeholders = ",".join("?" for _ in _RECOVERABLE_STATES)
    return conn.execute(
        f"SELECT t.txn_id, t.state FROM transactions t "
        f"WHERE t.state IN ({placeholders}) "
        f"AND EXISTS ("
        f"  SELECT 1 FROM effects e "
        f"  WHERE e.txn_id = t.txn_id AND e.status = ?"
        f") "
        f"ORDER BY t.created_at",
        (*sorted(_RECOVERABLE_STATES), EffectStatus.APPLIED.name),
    ).fetchall()


def _resume_one(
    conn: sqlite3.Connection,
    txn_id: str,
    prior_state: str,
    adapters: dict[str, Any],
    registry: ToolRegistry,
) -> TxnRecovery:
    """Resume the backward fold for one mid-flight transaction.

    Walk the durable effects newest-first (the same direction the live unwind
    folds). For each APPLIED effect:

    - reversible → the DB already auto-rolled-back the uncommitted txn on the
      crash; mark COMPENSATED with action ``db_auto_rolled_back``. No live
      restore is attempted (the savepoint is gone).
    - irreversible → re-fire the registered compensator with the effect's
      original args. Success flips the durable row to COMPENSATED *and commits*
      (the exactly-once fence). A missing or raising compensator leaves the row
      APPLIED and forces a STUCK landing.

    An effect already durably COMPENSATED (a prior recovery pass, or a partial
    in-process unwind before the crash) is skipped — that is the fence in
    action.
    """
    rows = conn.execute(
        "SELECT * FROM effects WHERE txn_id = ? ORDER BY idx DESC",
        (txn_id,),
    ).fetchall()

    outcomes: list[EffectRecovery] = []
    stuck = False

    for row in rows:
        effect = _effect_from_row(row)

        if effect.status is EffectStatus.COMPENSATED:
            # Already undone (prior pass or pre-crash unwind). The fence.
            outcomes.append(
                EffectRecovery(
                    effect_id=effect.effect_id,
                    index=effect.index,
                    tool=effect.tool,
                    reversible=effect.reversible,
                    action="already_compensated",
                )
            )
            continue

        if effect.status is not EffectStatus.APPLIED:
            # STAGED irreversible never fired; FAILED was the trigger; GATED
            # was denied. None of these put anything in the world to undo.
            continue

        if effect.reversible:
            # The DB already undid the uncommitted write on process death.
            # Record the fact honestly; do not touch a dead savepoint.
            _set_effect_status(conn, txn_id, effect.index, EffectStatus.COMPENSATED.name)
            outcomes.append(
                EffectRecovery(
                    effect_id=effect.effect_id,
                    index=effect.index,
                    tool=effect.tool,
                    reversible=True,
                    action="db_auto_rolled_back",
                )
            )
            continue

        # Irreversible APPLIED: the real recovery target. Resolve the
        # compensator from the registry by the effect's tool name.
        comp_name = _resolve_compensator_name(registry, effect.tool)
        if comp_name is None or comp_name not in registry:
            stuck = True
            outcomes.append(
                EffectRecovery(
                    effect_id=effect.effect_id,
                    index=effect.index,
                    tool=effect.tool,
                    reversible=False,
                    action="stuck_missing_compensator",
                    error=(
                        f"tool {effect.tool!r} has no registered compensator; "
                        f"the standing side effect requires manual recovery"
                    ),
                )
            )
            continue

        comp_spec = registry.get(comp_name)
        try:
            comp_adapter = adapters[comp_spec.resource]
        except KeyError:
            stuck = True
            outcomes.append(
                EffectRecovery(
                    effect_id=effect.effect_id,
                    index=effect.index,
                    tool=effect.tool,
                    reversible=False,
                    action="stuck_missing_compensator",
                    error=f"no adapter registered for resource {comp_spec.resource!r}",
                )
            )
            continue

        # Synthetic carrier for the compensator fire — not journalled as a
        # separate row, exactly as the live unwind does it.
        comp_effect = Effect(
            txn_id=txn_id,
            index=-1,
            tool=comp_name,
            args=effect.args,
            resource=comp_spec.resource,
            reversible=False,
        )
        try:
            comp_adapter.apply(comp_effect, comp_spec.fn)
        except Exception as exc:  # noqa: BLE001 - any compensator failure is STUCK
            # Intentionally broad: a compensator is third-party tool code and
            # may raise anything. Any failure means the inverse did NOT apply,
            # so the row stays APPLIED and the txn is STUCK — the operator must
            # intervene. Re-raising would abort recovery of *other* txns.
            stuck = True
            outcomes.append(
                EffectRecovery(
                    effect_id=effect.effect_id,
                    index=effect.index,
                    tool=effect.tool,
                    reversible=False,
                    action="stuck_compensator_raised",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        # Inverse applied. Flip + commit the fence BEFORE moving on, so a crash
        # right here cannot re-fire this compensator on the next pass.
        _set_effect_status(conn, txn_id, effect.index, EffectStatus.COMPENSATED.name)
        outcomes.append(
            EffectRecovery(
                effect_id=effect.effect_id,
                index=effect.index,
                tool=effect.tool,
                reversible=False,
                action="compensated",
            )
        )

    final = TxnState.STUCK.name if stuck else TxnState.ROLLED_BACK.name
    _set_txn_state(conn, txn_id, final)
    return TxnRecovery(
        txn_id=txn_id,
        prior_state=prior_state,
        final_state=final,
        effects=outcomes,
    )


def _resolve_compensator_name(registry: ToolRegistry, tool_name: str) -> str | None:
    """The compensator declared by ``tool_name``'s ToolSpec, or None.

    If the tool itself is no longer registered (e.g. recovery runs in a process
    that imported a different toolset), there is no way to know its inverse —
    return None and let the caller land STUCK.
    """
    if tool_name not in registry:
        return None
    return registry.get(tool_name).compensator


def recover(
    journal: AuditJournal | str,
    adapters: dict[str, Any],
    registry: ToolRegistry | None = None,
) -> RecoveryReport:
    """Resume every interrupted backward fold in a durable journal.

    The public entry point. Given the durable audit journal (an
    :class:`AuditJournal`, or a path to its SQLite file — the "fresh process
    after a crash" case), the live adapter map, and the tool registry, find
    every transaction left mid-flight and resume its backward fold to a
    terminal state.

    Idempotent by construction: re-running ``recover`` against the same DB is a
    no-op for any transaction whose effects are already terminal — the durable
    status is the exactly-once fence, so a compensator that fired on a prior
    pass never fires again.

    Args:
        journal: the durable audit journal (object or DB path).
        adapters: resource-name → adapter, same shape ``agent_txn`` takes. Only
            the adapters whose resources back live compensators are actually
            used; an irreversible compensator typically routes to an
            ``HTTPAdapter``.
        registry: the tool registry to resolve compensator names + callables.
            Defaults to the process-global :data:`pherix.core.tools.REGISTRY`.

    Returns:
        a :class:`RecoveryReport` describing what each recovered transaction
        did (compensators fired, DB auto-rollbacks, anything left STUCK).
    """
    registry = registry if registry is not None else _DEFAULT_REGISTRY
    conn, owned_journal = _open_db(journal)
    own_connection = owned_journal is None
    try:
        mid_flight = _find_mid_flight(conn)
        reports = [
            _resume_one(conn, row["txn_id"], row["state"], adapters, registry)
            for row in mid_flight
        ]
        return RecoveryReport(transactions=reports)
    finally:
        # Only close a connection we opened ourselves; never close one the
        # caller's AuditJournal still owns.
        if own_connection:
            conn.close()


__all__ = [
    "recover",
    "RecoveryReport",
    "TxnRecovery",
    "EffectRecovery",
]
