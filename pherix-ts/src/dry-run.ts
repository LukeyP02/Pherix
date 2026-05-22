/**
 * Speculative dry-run — fold forward against a snapshot, then discard.
 * Mirror of pherix/core/dry_run.py.
 *
 * A dry-run is mechanically `agentTxn` that rolls back at the end instead of
 * committing: the same forward fold over the same journal, against the same
 * adapters, with the existing begin/rollback bracket already giving "discard
 * the world" for free. The whole contribution is wiring:
 *
 * - A top-level `dryRun` entry point (its own register alongside `agentTxn`).
 * - A `DryRunResult` carrying the journal, the `wouldHaveFired` filter, the
 *   captured policy verdicts, and (for StateDiffable adapters) the state diff.
 * - Capture-mode policy: stage-time calls swap `Policy.evaluate` for
 *   `Policy.tryEvaluate`; commit-time uses `Policy.collectVerdicts`. Neither
 *   raises on Deny — verdicts flow into the result instead.
 *
 * Maths framing: a real transaction is a forward fold of the journal ending in
 * `adapter.commit`. A dry-run is the same forward fold ending in
 * `adapter.rollback` — the measurement without collapse. What you observe is
 * the journal-as-built (plus the verdicts that would fire); what survives is
 * nothing.
 */

import {
  isStateDiffable,
  isTransactionalAdapter,
  type ResourceAdapter,
  type StateDiffable,
  type TransactionalResourceAdapter,
} from "./adapters/base.js";
import { AuditJournal } from "./audit.js";
import type { Effect } from "./effects.js";
import { Policy, type PolicyVerdict } from "./policy.js";
import { activeTxn } from "./tools.js";
import { TxnContext, type TxnContextOptions } from "./runtime.js";

/** Distinct adapter instances (one adapter may serve several resource keys). */
function uniqueAdapters(adapters: Record<string, ResourceAdapter>): ResourceAdapter[] {
  const seen = new Set<ResourceAdapter>();
  const out: ResourceAdapter[] = [];
  for (const a of Object.values(adapters)) {
    if (!seen.has(a)) {
      seen.add(a);
      out.push(a);
    }
  }
  return out;
}

/**
 * The product of a dry-run — observation layers, no side effects.
 *
 * `journal` is the per-effect record exactly as a real `agentTxn` would have
 * produced; the dry-run *did* fold forward, the rollback at the end is what
 * makes it dry. `wouldHaveFired` is the slice filtered by `(reversible=false,
 * status=STAGED)` — the irreversibles that would have fired at commit-time;
 * their apply functions never ran. `policyVerdicts` is the flat list of every
 * captured verdict: stage-time (one per rule/cap per effect, in body order)
 * then commit-time (in journal order). `isClean` is the conjunction of every
 * verdict. `stateDiff` is the per-resource structural delta keyed by adapter
 * name, populated only for StateDiffable adapters (empty for the irreversible
 * HTTP adapter, whose structural record is `wouldHaveFired`).
 */
export class DryRunResult {
  readonly txnId: string;
  readonly journal: Effect[];
  readonly wouldHaveFired: Effect[];
  readonly policyVerdicts: PolicyVerdict[];
  readonly isClean: boolean;
  readonly stateDiff: Record<string, Record<string, unknown>>;

  constructor(opts: {
    txnId: string;
    journal: Effect[];
    wouldHaveFired: Effect[];
    policyVerdicts: PolicyVerdict[];
    isClean: boolean;
    stateDiff?: Record<string, Record<string, unknown>>;
  }) {
    this.txnId = opts.txnId;
    this.journal = opts.journal;
    this.wouldHaveFired = opts.wouldHaveFired;
    this.policyVerdicts = opts.policyVerdicts;
    this.isClean = opts.isClean;
    this.stateDiff = opts.stateDiff ?? {};
  }
}

export interface DryRunOptions extends TxnContextOptions {
  policy?: Policy;
  audit?: AuditJournal;
}

/**
 * Speculative-execution entry point. Inside the body `fn`, the agent's tool
 * calls intercept exactly as under `agentTxn` — same journalling, same
 * snapshots, same StagedResult sentinels for irreversibles. On exit, the
 * snapshot/rollback bracket discards the world and the fully-populated
 * DryRunResult lands on `ctx.result`.
 *
 * Policy denial during the body does NOT abort the run — the verdict is
 * captured into the result instead, and the body keeps running so the full
 * journal materialises. Genuine errors (adapter failures) still reject as in a
 * normal txn; a thrown body means there is no result to inspect.
 */
export async function dryRun(
  adapters: Record<string, ResourceAdapter>,
  fn: (ctx: TxnContext) => unknown | Promise<unknown>,
  options: DryRunOptions = {},
): Promise<TxnContext> {
  const policy = options.policy ?? Policy.allowAll();
  const audit = options.audit ?? AuditJournal.inMemory();

  const unique = uniqueAdapters(adapters);
  for (const adapter of unique) {
    if (isTransactionalAdapter(adapter)) await (adapter as TransactionalResourceAdapter).begin();
  }

  // Capture a read-only state baseline per StateDiffable adapter, after begin so
  // the txn's transaction bracket is active. Diffed at the finalise hook.
  const stateBaselines: Array<[StateDiffable, unknown]> = [];
  for (const adapter of unique) {
    if (isStateDiffable(adapter)) {
      stateBaselines.push([adapter, await adapter.stateBaseline()]);
    }
  }

  const ctx = new TxnContext(adapters, policy, audit, {
    dryRun: true,
    clientId: options.clientId ?? null,
    stateBaselines,
  });

  return activeTxn.run(ctx, async () => {
    try {
      await fn(ctx);
      if (!ctx.finished) await ctx.dryRunFinalise();
    } catch (e) {
      // Genuine error in the body: unwind cleanly. No result materialises.
      if (!ctx.finished) await ctx.rollback();
      throw e;
    }
    return ctx;
  });
}
