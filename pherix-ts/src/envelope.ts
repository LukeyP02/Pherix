/**
 * Longitudinal envelope — durable, cross-run spend caps (#10).
 * Mirror of pherix/core/envelope.py.
 *
 * A `Cap` in policy.ts is per-transaction: its running total lives on the
 * PolicyContext the runtime rebuilds for every agentTxn. So "≤ 3 charges" means
 * "≤ 3 charges in this one transaction" — the budget resets when the txn exits.
 *
 * A *longitudinal* cap folds the same contribution over the cross-session
 * journal instead, materialised as one running total per `(capName, periodKey)`
 * in a durable SQLite table (a sibling table inside the existing audit-journal
 * database — one host, one DB file). The mental model is unchanged from the
 * per-txn cap: "would this effect push the running total above max?" — only the
 * total now lives on disk and survives process restart.
 *
 * Budget consumption is commit-only. A cap that denied never ran; a txn that
 * rolled back never spent. So `evaluate` only ever *reads* the persisted total
 * and compares — it never writes. The increment is applied separately, by the
 * runtime, exactly once, on a successful commit.
 *
 * Known limitation — cross-process cap races (single-host). The read → decide →
 * flush window is not atomic across processes, so two processes can each pass a
 * charge against the same baseline and overshoot. Within one process the cap is
 * exact; across processes it is best-effort. Hard cross-process budget
 * enforcement belongs to the #12 control plane.
 */

import Database from "better-sqlite3";
import type { Effect } from "./effects.js";
import { Allow, Deny, type NamedRule, type Verdict } from "./policy.js";
import type { AuditJournal } from "./audit.js";

// -- period keys -------------------------------------------------------------

export type PeriodFn = () => string;

/** UTC calendar date as the period bucket — the default cap window. Two effects
 *  on the same UTC day share a bucket; midnight UTC rolls it over. */
export function dayPeriod(now?: Date): string {
  return (now ?? new Date()).toISOString().slice(0, 10);
}

/** A single, never-rolling bucket — "across every run, forever". */
export function allTimePeriod(): string {
  return "all-time";
}

// -- the durable total store -------------------------------------------------

const ENVELOPE_SCHEMA = `
CREATE TABLE IF NOT EXISTS envelope_totals (
    cap_name   TEXT NOT NULL,
    period_key TEXT NOT NULL,
    total      REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cap_name, period_key)
)
`;

/**
 * Durable running-total store — the longitudinal cap's persisted fold. Holds
 * one row per `(capName, periodKey)`: the cumulative contribution of every
 * committed transaction whose effects matched the cap, within that period.
 * Reading the total is one indexed lookup; the engine never re-walks history.
 */
export class EnvelopeStore {
  private readonly db: Database.Database;

  constructor(db: Database.Database) {
    this.db = db;
    this.db.exec(ENVELOPE_SCHEMA);
  }

  /** Open an independent connection to the SQLite file at `path` — production
   *  callers who hold a path, and the cross-restart test where a fresh handle
   *  must observe totals written by an earlier handle (process-death sim). */
  static fromPath(path: string): EnvelopeStore {
    return new EnvelopeStore(new Database(path));
  }

  /** Reuse an AuditJournal's connection — durable envelope state is a sibling
   *  table in the audit DB (one host, one file). */
  static fromAudit(audit: AuditJournal): EnvelopeStore {
    return new EnvelopeStore(audit.connection);
  }

  /** The persisted running total for `(capName, periodKey)`. Absent row → 0
   *  ("nothing spent this period yet"), never null. */
  total(capName: string, periodKey: string): number {
    const row = this.db
      .prepare("SELECT total FROM envelope_totals WHERE cap_name = ? AND period_key = ?")
      .get(capName, periodKey) as { total: number } | undefined;
    return row === undefined ? 0 : Number(row.total);
  }

  /** Atomically add `increment` to the period's total; return the new total.
   *  UPSERT with RETURNING so the bump is race-free against a second connection
   *  to the same file. Called by the runtime only after a successful commit. */
  add(capName: string, periodKey: string, increment: number): number {
    const row = this.db
      .prepare(
        "INSERT INTO envelope_totals (cap_name, period_key, total, updated_at) " +
          "VALUES (?, ?, ?, ?) " +
          "ON CONFLICT(cap_name, period_key) DO UPDATE " +
          "SET total = total + excluded.total, updated_at = excluded.updated_at " +
          "RETURNING total",
      )
      .get(capName, periodKey, increment, new Date().toISOString()) as { total: number };
    return Number(row.total);
  }
}

// -- durable caps (rule objects) ---------------------------------------------

/** The journal-so-far contribution of matching effects, EXCLUDING the effect
 *  currently being evaluated (by identity). At stage-time the candidate is not
 *  yet in the journal (nothing excluded); at commit-time it is (excluded once),
 *  so the caller's `+ contribution(current)` is never double-counted. */
function inTxnContribution(cap: NamedRule, ctx: { journal?: readonly Effect[] }, current: Effect): number {
  const journal = ctx.journal ?? [];
  let total = 0;
  for (const e of journal) {
    if (e !== current && cap.appliesTo(e)) total += cap.contribution(e);
  }
  return total;
}

class DurableCountCap implements NamedRule {
  constructor(
    readonly tool: string,
    readonly max: number,
    readonly store: EnvelopeStore,
    readonly period: PeriodFn,
    readonly label: string | null,
  ) {}

  get name(): string {
    return this.label ?? `DurableCap.count(tool=${JSON.stringify(this.tool)}, max=${this.max})`;
  }

  appliesTo(effect: Effect): boolean {
    return effect.tool === this.tool;
  }

  contribution(_effect: Effect): number {
    return 1;
  }

  evaluate(effect: Effect, ctx: { journal?: readonly Effect[] }): Verdict {
    if (!this.appliesTo(effect)) return Allow();
    // Persisted baseline (prior committed runs, this period) plus the
    // contributions already journalled in THIS txn, so a third charge in one
    // run is denied even before commit, exactly like the per-txn cap.
    const baseline = this.store.total(this.name, this.period());
    const inTxn = inTxnContribution(this, ctx, effect);
    if (baseline + inTxn + this.contribution(effect) > this.max) {
      return Deny(
        `would exceed durable count cap (max=${this.max}) for tool ${JSON.stringify(this.tool)} ` +
          `in period ${JSON.stringify(this.period())}; persisted=${baseline}, this-txn-so-far=${inTxn}`,
      );
    }
    return Allow();
  }
}

class DurableSumCap implements NamedRule {
  constructor(
    readonly tool: string,
    readonly via: (args: Record<string, unknown>) => number,
    readonly max: number,
    readonly store: EnvelopeStore,
    readonly period: PeriodFn,
    readonly label: string | null,
  ) {}

  get name(): string {
    return this.label ?? `DurableCap.sum(tool=${JSON.stringify(this.tool)}, max=${this.max})`;
  }

  appliesTo(effect: Effect): boolean {
    return effect.tool === this.tool;
  }

  contribution(effect: Effect): number {
    return this.via(effect.args);
  }

  evaluate(effect: Effect, ctx: { journal?: readonly Effect[] }): Verdict {
    if (!this.appliesTo(effect)) return Allow();
    const baseline = this.store.total(this.name, this.period());
    const inTxn = inTxnContribution(this, ctx, effect);
    const candidate = baseline + inTxn + this.contribution(effect);
    if (candidate > this.max) {
      return Deny(
        `would exceed durable sum cap (max=${this.max}) for tool ${JSON.stringify(this.tool)} ` +
          `in period ${JSON.stringify(this.period())}; persisted=${baseline}, ` +
          `this-txn-so-far=${inTxn}, contribution=${this.contribution(effect)}`,
      );
    }
    return Allow();
  }
}

/** Namespace for longitudinal (durable, cross-run) cap primitives. Mirrors
 *  `Cap`, but every cap is bound to an EnvelopeStore + period function. The
 *  returned objects are rules — register them via `policy.addCap(...)` exactly
 *  like the per-txn caps; the engine treats them identically. */
export const DurableCap = {
  count(opts: {
    tool: string;
    max: number;
    store: EnvelopeStore;
    period?: PeriodFn;
    label?: string;
  }): NamedRule {
    return new DurableCountCap(
      opts.tool,
      opts.max,
      opts.store,
      opts.period ?? dayPeriod,
      opts.label ?? null,
    );
  },
  sum(opts: {
    tool: string;
    via: (args: Record<string, unknown>) => number;
    max: number;
    store: EnvelopeStore;
    period?: PeriodFn;
    label?: string;
  }): NamedRule {
    return new DurableSumCap(
      opts.tool,
      opts.via,
      opts.max,
      opts.store,
      opts.period ?? dayPeriod,
      opts.label ?? null,
    );
  },
};

// -- commit-time increment fold ---------------------------------------------

/** One pending durable-cap flush: `add(capName, periodKey, amount)`. Carries
 *  `store` so the runtime can flush a policy whose caps are bound to different
 *  stores without re-deriving which store each cap used. */
export interface EnvelopeIncrement {
  store: EnvelopeStore;
  capName: string;
  periodKey: string;
  amount: number;
}

/** Whether `obj` is a longitudinal cap (carries a store + period). */
export function isDurableCap(obj: unknown): obj is DurableCountCap | DurableSumCap {
  return obj instanceof DurableCountCap || obj instanceof DurableSumCap;
}

/** Fold a committed txn's journal into the per-cap durable increments. For each
 *  durable cap, sum the contribution of every matching effect in the final
 *  journal — this transaction's consumption of the cap's budget. The period key
 *  is snapped once per cap at the commit instant. The runtime calls this only on
 *  a successful commit; a rolled-back or denied txn never reaches it, so budget
 *  is consumed exactly when effects actually landed. */
export function pendingIncrements(durableCaps: NamedRule[], journal: Effect[]): EnvelopeIncrement[] {
  const out: EnvelopeIncrement[] = [];
  for (const cap of durableCaps) {
    if (!isDurableCap(cap)) continue;
    let amount = 0;
    for (const e of journal) {
      if (cap.appliesTo(e)) amount += cap.contribution(e);
    }
    if (amount === 0) continue;
    out.push({ store: cap.store, capName: cap.name, periodKey: cap.period(), amount });
  }
  return out;
}

/** Apply each pending increment to its store. The runtime's commit hook —
 *  separated from pendingIncrements so the deltas are computed before but
 *  persisted only once the commit is known to have succeeded. */
export function flushIncrements(increments: EnvelopeIncrement[]): void {
  for (const inc of increments) inc.store.add(inc.capName, inc.periodKey, inc.amount);
}
