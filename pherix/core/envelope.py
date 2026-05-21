"""Longitudinal envelope — durable, cross-run spend caps (#10).

A ``Cap`` in :mod:`pherix.core.policy` is per-transaction: its running total
lives on the :class:`~pherix.core.policy.PolicyContext`, which the runtime
rebuilds for every ``agent_txn``. So "≤ 3 charges" means "≤ 3 charges *in this
one transaction*" — the budget resets the instant the with-block exits, and a
fresh process knows nothing of what prior runs spent.

A *longitudinal* cap folds the same contribution over the **cross-session**
journal instead. The fold is materialised as a single running total per
``(cap_name, period_key)`` in a durable SQLite table, so the engine never has to
re-walk every historical transaction to answer "how much have we spent today?"
— it reads one row. The mental model is unchanged from the per-txn cap: a cap is
a predicate "would this effect push the running total above ``max``?" — only the
*total* now lives on disk and survives process restart.

Two pieces:

- :class:`EnvelopeStore` — the durable total store. Persists running totals in
  an ``envelope_totals`` sibling table inside the **existing audit-journal
  SQLite database** (single host, one DB file — no second ``.db``). Clean API:
  :meth:`total` reads, :meth:`add` increments, both scoped by a period key.
- :class:`_DurableCountCap` / :class:`_DurableSumCap` — rule objects with the
  same ``(applies_to, contribution, evaluate)`` shape as the per-txn caps, but
  whose baseline is read from the store. Constructed via
  :meth:`DurableCap.count` / :meth:`DurableCap.sum`.

**Budget consumption is commit-only.** A cap that *denied* never ran; a txn that
*rolled back* never spent. So ``evaluate`` only ever *reads* the persisted total
and compares — it never writes. The increment is applied separately, by the
runtime, exactly once, on a **successful commit** (see the integration note in
the PR body / module docstring of the test). :meth:`pending_increments` folds
this txn's journal into the per-cap deltas the runtime should flush; the runtime
calls :meth:`EnvelopeStore.add` for each only after the commit lands.

The period key turns "per day" / "per hour" / "for all time" into a pure
function of the wall clock at evaluation time — :func:`day_period` is the
default (UTC calendar date). Two effects in the same UTC day share a bucket;
midnight UTC rolls the budget over with no code change.

Known limitation — cross-process cap races (single-host)
========================================================
``evaluate`` reads the persisted baseline and decides; the increment is flushed
*after* commit. That read -> decide -> flush window is NOT atomic across
processes, so two processes that both observe "spent 90 / cap 100" can each pass
a 90-unit charge and both flush, overshooting to 180. The #8 intent mechanism
guards resource KEYS, not cap TOTALS, so it does not close this. Within one
process the cap is exact; across processes it is best-effort. Hard cross-process
budget enforcement belongs to the #12 control plane (the natural cross-process
arbiter, the same tier as cross-host isolation) — revisit it there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pherix.core.effects import Effect
from pherix.core.policy import Allow, Deny, Verdict


# -- period keys -------------------------------------------------------------


def day_period(now: datetime | None = None) -> str:
    """UTC calendar date as the period bucket — the default cap window.

    ``now`` defaults to the current UTC instant. Two effects evaluated on the
    same UTC day map to the same key and share one budget; the first effect
    after midnight UTC sees a fresh (empty) bucket. Returned as an ISO date
    string (``"2026-05-21"``) so it is human-legible in the audit DB.
    """
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def all_time_period(now: datetime | None = None) -> str:
    """A single, never-rolling bucket — "across every run, forever"."""
    return "all-time"


PeriodFn = Callable[[], str]


# -- the durable total store -------------------------------------------------


_ENVELOPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS envelope_totals (
    cap_name   TEXT NOT NULL,
    period_key TEXT NOT NULL,
    total      REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cap_name, period_key)
)
"""


class EnvelopeStore:
    """Durable running-total store — the longitudinal cap's persisted fold.

    Holds one row per ``(cap_name, period_key)``: the cumulative contribution
    of every *committed* transaction whose effects matched the cap, within that
    period. Reading the total is one indexed lookup; the engine never re-walks
    history.

    **Where the data lives (locked decision).** The totals are a sibling table
    inside the existing :class:`~pherix.core.audit.AuditJournal` SQLite database
    — single host, one DB file. Construct via :meth:`from_audit` to reuse the
    journal's own connection (so writes share the journal's transaction
    boundary on the same file), or via :meth:`from_path` / the ``path``
    constructor to open an independent connection to the same on-disk file —
    which is exactly what the "simulated restart" test does: a fresh handle on
    the same path must see prior runs' spend.

    The store is deliberately tiny and dependency-free: stdlib :mod:`sqlite3`
    only, matching the kernel's no-third-party rule.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.execute(_ENVELOPE_SCHEMA)
        self._conn.commit()

    @classmethod
    def from_path(cls, path: str) -> "EnvelopeStore":
        """Open an independent connection to the SQLite file at ``path``.

        Used both for production callers who hold a path and for the
        cross-restart test, where a *new* handle on the same path must observe
        totals written by an earlier handle (process-death simulation).
        """
        conn = sqlite3.connect(path)
        return cls(conn)

    @classmethod
    def from_audit(cls, audit: Any) -> "EnvelopeStore":
        """Reuse an :class:`~pherix.core.audit.AuditJournal`'s connection.

        The locked decision: durable envelope state is a sibling table in the
        audit-journal DB. ``audit`` exposes its connection via the private
        ``_conn`` attribute; we read it here so the runtime can hand the
        journal it already holds and get a store on the same file with no
        second connection. (Reaching for ``_conn`` is the deliberate seam — the
        envelope is part of the same durability domain as the journal, not a
        foreign consumer.)
        """
        return cls(audit._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def total(self, cap_name: str, period_key: str) -> float:
        """The persisted running total for ``(cap_name, period_key)``.

        Absent row → ``0.0`` ("nothing spent in this period yet"), never
        ``None`` — so a cap's arithmetic is total-clean on the first effect of
        a new period.
        """
        row = self._conn.execute(
            "SELECT total FROM envelope_totals "
            "WHERE cap_name = ? AND period_key = ?",
            (cap_name, period_key),
        ).fetchone()
        return 0.0 if row is None else float(row[0])

    def add(self, cap_name: str, period_key: str, increment: float) -> float:
        """Atomically add ``increment`` to the period's total; return the new total.

        UPSERT with ``RETURNING`` so the bump is race-free against a second
        connection to the same on-disk file (two processes flushing the same
        cap in the same period). Called by the runtime **only after a
        successful commit** — a denied cap or a rolled-back txn must not consume
        budget, so this is never called on the deny / rollback path.
        """
        cur = self._conn.execute(
            "INSERT INTO envelope_totals (cap_name, period_key, total, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(cap_name, period_key) DO UPDATE "
            "SET total = total + excluded.total, updated_at = excluded.updated_at "
            "RETURNING total",
            (cap_name, period_key, float(increment), _now()),
        )
        new_total = float(cur.fetchone()[0])
        self._conn.commit()
        return new_total

    def close(self) -> None:
        self._conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- durable caps (rule objects) ---------------------------------------------


@dataclass
class _DurableCountCap:
    """Cross-run cap on the number of times a tool fires within a period.

    Same shape as :class:`pherix.core.policy._CountCap`, but the running total
    is ``store.total(name, period)`` + this-txn's prior matching effects,
    instead of a per-txn :class:`PolicyContext` bucket. ``evaluate`` is
    read-only; the commit increment is flushed separately by the runtime.
    """

    tool: str
    max: int
    store: EnvelopeStore
    period: PeriodFn = day_period
    label: str | None = None

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        return f"DurableCap.count(tool={self.tool!r}, max={self.max})"

    def applies_to(self, effect: Effect) -> bool:
        return effect.tool == self.tool

    def contribution(self, effect: Effect) -> float:
        return 1.0

    def evaluate(self, effect: Effect, ctx: Any) -> Verdict:
        if not self.applies_to(effect):
            return Allow()
        # The persisted baseline (prior committed runs, this period) plus the
        # contributions already journalled in THIS txn (so a third charge in
        # one run is denied even before commit, exactly like the per-txn cap).
        baseline = self.store.total(self.name, self.period())
        in_txn = self._in_txn_contribution(effect, ctx)
        if baseline + in_txn + self.contribution(effect) > self.max:
            return Deny(
                f"would exceed durable count cap (max={self.max}) for tool "
                f"{self.tool!r} in period {self.period()!r}; "
                f"persisted={baseline}, this-txn-so-far={in_txn}"
            )
        return Allow()

    def _in_txn_contribution(self, effect: Effect, ctx: Any) -> float:
        return _in_txn_count(self, ctx, effect)


@dataclass
class _DurableSumCap:
    """Cross-run cap on a tool's cumulative numeric contribution within a period.

    ``via(args)`` extracts the per-fire contribution (e.g. charge amount). Same
    read-only / commit-flush split as :class:`_DurableCountCap`.
    """

    tool: str
    via: Callable[[dict], float | int]
    max: float | int
    store: EnvelopeStore
    period: PeriodFn = day_period
    label: str | None = None

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        return f"DurableCap.sum(tool={self.tool!r}, max={self.max})"

    def applies_to(self, effect: Effect) -> bool:
        return effect.tool == self.tool

    def contribution(self, effect: Effect) -> float:
        return float(self.via(effect.args))

    def evaluate(self, effect: Effect, ctx: Any) -> Verdict:
        if not self.applies_to(effect):
            return Allow()
        baseline = self.store.total(self.name, self.period())
        in_txn = _in_txn_sum(self, ctx, effect)
        candidate = baseline + in_txn + self.contribution(effect)
        if candidate > self.max:
            return Deny(
                f"would exceed durable sum cap (max={self.max}) for tool "
                f"{self.tool!r} in period {self.period()!r}; "
                f"persisted={baseline}, this-txn-so-far={in_txn}, "
                f"contribution={self.contribution(effect)}"
            )
        return Allow()


def _in_txn_count(cap: _DurableCountCap, ctx: Any, current: Effect) -> float:
    """Matching effects already in this txn's journal (the journal-so-far fold),
    EXCLUDING the effect currently being evaluated.

    ``ctx.journal`` is the runtime's live journal snapshot; at stage-time the
    candidate effect is not yet in it, at commit-time the whole journal is —
    INCLUDING ``current``. The caller adds ``contribution(current)`` exactly
    once on top of this fold, so the fold must not also count ``current`` or
    the commit-time re-walk would double-count it (90 + 90 > 100 for a single
    90 charge). Excluding by object identity is correct on both walks:
    stage-time the candidate isn't in the journal (nothing excluded);
    commit-time it is (excluded once). We count prior matching effects so
    multiple fires within ONE run accumulate on top of the persisted baseline.
    """
    journal = getattr(ctx, "journal", ()) or ()
    return float(sum(1 for e in journal if cap.applies_to(e) and e is not current))


def _in_txn_sum(cap: _DurableSumCap, ctx: Any, current: Effect) -> float:
    journal = getattr(ctx, "journal", ()) or ()
    return float(
        sum(
            cap.contribution(e)
            for e in journal
            if cap.applies_to(e) and e is not current
        )
    )


class DurableCap:
    """Namespace for longitudinal (durable, cross-run) cap primitives.

    Mirrors :class:`pherix.core.policy.Cap`, but every cap is bound to an
    :class:`EnvelopeStore` and a period function. The returned objects are
    rules — register them on a :class:`~pherix.core.policy.Policy` via
    ``policy.add_cap(...)`` exactly like the per-txn caps; the policy engine
    treats them identically (it only calls ``applies_to`` / ``contribution`` /
    ``evaluate``).
    """

    @staticmethod
    def count(
        *,
        tool: str,
        max: int,
        store: EnvelopeStore,
        period: PeriodFn = day_period,
        label: str | None = None,
    ) -> _DurableCountCap:
        return _DurableCountCap(
            tool=tool, max=max, store=store, period=period, label=label
        )

    @staticmethod
    def sum(
        *,
        tool: str,
        via: Callable[[dict], float | int],
        max: float | int,
        store: EnvelopeStore,
        period: PeriodFn = day_period,
        label: str | None = None,
    ) -> _DurableSumCap:
        return _DurableSumCap(
            tool=tool, via=via, max=max, store=store, period=period, label=label
        )


# -- commit-time increment fold ---------------------------------------------


@dataclass(frozen=True)
class EnvelopeIncrement:
    """One pending durable-cap flush: ``add(cap_name, period_key, amount)``.

    Produced by :func:`pending_increments` from a committed txn's journal;
    consumed by the runtime's post-commit hook, which calls
    :meth:`EnvelopeStore.add` once per increment. Carrying ``store`` on the
    increment lets the runtime flush a policy with caps bound to *different*
    stores without re-deriving which store each cap used.
    """

    store: EnvelopeStore
    cap_name: str
    period_key: str
    amount: float


def pending_increments(
    durable_caps: list[Any], journal: list[Effect]
) -> list[EnvelopeIncrement]:
    """Fold a committed txn's journal into the per-cap durable increments.

    For each durable cap, sum the contribution of every matching effect in the
    final journal — that is this transaction's consumption of the cap's budget.
    The period key is snapped *once* per cap at flush time (the commit instant),
    so a txn that straddles midnight UTC bills the whole txn to the period in
    which it committed — a deliberate, simple choice for single-host caps.

    The runtime calls this only on a successful commit and then applies each
    increment via :meth:`EnvelopeStore.add`. A rolled-back or denied txn never
    reaches this fold, so budget is consumed exactly when — and only when —
    effects actually landed.
    """
    out: list[EnvelopeIncrement] = []
    for cap in durable_caps:
        store = getattr(cap, "store", None)
        if store is None:
            continue
        amount = float(
            sum(
                cap.contribution(e)
                for e in journal
                if cap.applies_to(e)
            )
        )
        if amount == 0:
            continue
        out.append(
            EnvelopeIncrement(
                store=store,
                cap_name=cap.name,
                period_key=cap.period(),
                amount=amount,
            )
        )
    return out


def flush_increments(increments: list[EnvelopeIncrement]) -> None:
    """Apply each pending increment to its store. The runtime's commit hook.

    Separated from :func:`pending_increments` so the runtime can compute the
    deltas at any point but only persist them once the commit is known to have
    succeeded (the totals are durable the instant they are written).
    """
    for inc in increments:
        inc.store.add(inc.cap_name, inc.period_key, inc.amount)


def is_durable_cap(obj: Any) -> bool:
    """Whether ``obj`` is a longitudinal cap (carries a store + period)."""
    return isinstance(obj, (_DurableCountCap, _DurableSumCap))


__all__ = [
    "DurableCap",
    "EnvelopeIncrement",
    "EnvelopeStore",
    "all_time_period",
    "day_period",
    "flush_increments",
    "is_durable_cap",
    "pending_increments",
]
