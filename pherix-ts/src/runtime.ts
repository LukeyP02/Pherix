/**
 * The orchestration — agentTxn() and the interception entry point.
 * Mirror of pherix/core/runtime.py.
 *
 * agentTxn() opens a Transaction, binds a TxnContext into the activeTxn
 * async-local store, and drives every intercepted tool call through the right
 * lane:
 *
 * - reversible lane: policy -> snapshot -> apply -> journal. Effects run live;
 *   rollback() folds the journal backward, restoring each snapshot newest-first.
 * - irreversible lane: policy -> stage. The effect is recorded as intent and the
 *   agent receives a StagedResult(effectId) sentinel. commit() re-checks policy
 *   (TOCTOU), checks the gate (every staged irreversible must be
 *   compensator-backed or pre-approved via approveIrreversible), then fires
 *   staged irreversibles in journal index order. A mid-fire failure triggers a
 *   mixed-fold backward unwind: compensator(effect) for already-fired
 *   irreversibles, adapter.restore(snapshot) for already-applied reversibles.
 *   Terminal state is ROLLED_BACK if every unwind step succeeded, STUCK if any
 *   compensator was missing or itself raised.
 */

import {
  isTransactionalAdapter,
  type ResourceAdapter,
  type StateDiffable,
  type TransactionalResourceAdapter,
} from "./adapters/base.js";
import { AuditJournal } from "./audit.js";
import { DryRunResult } from "./dry-run.js";
import { Effect, EffectStatus, StagedResult } from "./effects.js";
import { Policy, PolicyContext, PolicyViolation, type PolicyVerdict, sqlReader } from "./policy.js";
import { REGISTRY, activeEffect, activeTxn, type RecordingContext } from "./tools.js";
import { Transaction, TxnState } from "./transaction.js";

/** Raised at stage-time when a tool declares a compensator that does not exist.
 *  Catching the typo before any state changes turns a silent STUCK-on-rollback
 *  into a loud error. */
export class CompensatorNotRegistered extends Error {
  constructor(
    public readonly compensator: string,
    public readonly tool: string,
  ) {
    super(
      `tool ${JSON.stringify(tool)} declares compensator ${JSON.stringify(compensator)}, ` +
        `but no tool of that name is registered. The compensator must itself be a ` +
        `registered tool.`,
    );
    this.name = "CompensatorNotRegistered";
  }
}

/** Raised at commit-time when staged irreversibles need pre-approval. After a
 *  gate-block the transaction is unwound and ends in ROLLED_BACK. */
export class GateBlocked extends Error {
  needsApproval: string[];
  constructor(needsApproval: string[]) {
    super(
      "commit blocked at the gate; the following staged irreversible effects " +
        "need approveIrreversible() or a registered compensator: " +
        needsApproval.join(", "),
    );
    this.name = "GateBlocked";
    this.needsApproval = [...needsApproval];
  }
}

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

export interface TxnContextOptions {
  clientId?: string | null;
  /** Dry-run mode: capture policy verdicts instead of raising, and finalise by
   *  rolling back. Set by `dryRun()`, not by `agentTxn()`. */
  dryRun?: boolean;
  /** Per-StateDiffable-adapter baselines captured at txn begin (dry-run only),
   *  diffed at the finalise hook before the rollback discards the world. */
  stateBaselines?: Array<[StateDiffable, unknown]>;
}

/** The active-transaction object stored in the activeTxn async-local store. The
 *  tool wrapper calls recordToolCall on this — the single interception entry. */
export class TxnContext implements RecordingContext {
  readonly txn: Transaction;
  readonly audit: AuditJournal;
  private readonly adapters: Record<string, ResourceAdapter>;
  private readonly policy: Policy;
  private readonly approvals = new Set<string>();
  private readonly policyCtx: PolicyContext;
  private readonly clientId: string | null;
  private finishedFlag = false;
  /** Dry-run mode: stage-time policy captures verdicts instead of raising, and
   *  the txn finalises by rolling back. */
  private readonly dryRunMode: boolean;
  private readonly stateBaselines: Array<[StateDiffable, unknown]>;
  private readonly stageVerdicts: PolicyVerdict[] = [];
  /** The dry-run product, populated by `dryRunFinalise`. Null otherwise. */
  result: DryRunResult | null = null;

  constructor(
    adapters: Record<string, ResourceAdapter>,
    policy: Policy,
    audit: AuditJournal,
    options: TxnContextOptions = {},
  ) {
    this.txn = new Transaction();
    this.txn.policy = policy;
    this.audit = audit;
    this.adapters = adapters;
    this.policy = policy;
    this.clientId = options.clientId ?? null;
    this.dryRunMode = options.dryRun ?? false;
    this.stateBaselines = options.stateBaselines ?? [];
    // One PolicyContext per txn: carries the journal-so-far reference + per-cap
    // running totals across every stage-time evaluate(), reused for the
    // commit-time evaluateJournal walk (which resets caps and re-folds).
    // The reader is a closure over the live adapter map, so a world-state-aware
    // rule's ctx.read(resource, key) queries the right adapter's live committed
    // state — the substrate for #7's TOCTOU divergence between the two walks.
    this.policyCtx = new PolicyContext({
      journal: this.txn.effects,
      where: "stage",
      reader: sqlReader(adapters),
    });
    this.audit.recordTransaction(this.txn, {
      clientId: this.clientId,
      dryRun: this.dryRunMode,
    });
  }

  get txnId(): string {
    return this.txn.txnId;
  }

  get finished(): boolean {
    return this.finishedFlag;
  }

  private guardOpen(): void {
    if (!this.txn.isOpen) {
      throw new Error(
        `transaction ${this.txn.txnId} is ${this.txn.state}, not open`,
      );
    }
  }

  private resolveAdapter(resource: string): ResourceAdapter {
    const adapter = this.adapters[resource];
    if (adapter === undefined) {
      throw new Error(
        `no adapter registered for resource ${JSON.stringify(resource)}; ` +
          `known resources: ${Object.keys(this.adapters).join(", ")}`,
      );
    }
    return adapter;
  }

  // --- interception ---

  async recordToolCall(toolName: string, args: Record<string, unknown>): Promise<unknown> {
    this.guardOpen();
    const spec = REGISTRY.get(toolName);

    // Validate compensator names at stage-time, before any state changes.
    if (spec.compensator !== null && !REGISTRY.has(spec.compensator)) {
      throw new CompensatorNotRegistered(spec.compensator, toolName);
    }

    const adapter = this.resolveAdapter(spec.resource);
    const effect = new Effect({
      txnId: this.txn.txnId,
      index: this.txn.nextIndex(),
      tool: toolName,
      args,
      resource: spec.resource,
      // The adapter's honesty flag is the truth, not the @tool declaration.
      reversible: adapter.supportsRollback(),
      compensator: spec.compensator,
    });

    // Stage-time policy. In a normal txn: raise on first Deny, before
    // journalling (so a denied effect leaves no journal entry). In a dry-run:
    // capture the verdicts without raising, so the body keeps running and the
    // full journal materialises for the final DryRunResult.
    if (this.dryRunMode) {
      this.stageVerdicts.push(...(await this.policy.tryEvaluate(effect, this.policyCtx, "stage")));
    } else {
      await this.policy.evaluate(effect, this.policyCtx, "stage");
    }

    this.txn.addEffect(effect);
    this.audit.recordEffect(effect);

    // Irreversible lane: stage without applying.
    if (!effect.reversible) {
      const staged = new StagedResult(effect.effectId);
      effect.result = staged;
      effect.status = EffectStatus.STAGED;
      this.audit.updateEffect(effect);
      return staged;
    }

    // Reversible lane: snapshot -> apply. The apply is awaited so an async tool
    // (the normal case for TS DB/HTTP clients) is fully resolved before the
    // effect is marked APPLIED, and a rejection lands in the catch — driving
    // FAILED status + the unwind, exactly like a synchronous throw. The await
    // sits inside activeEffect.run so the async-local store survives the tool's
    // internal await boundaries.
    effect.snapshot = await adapter.snapshot(effect);
    try {
      effect.result = await activeEffect.run(effect, async () => adapter.apply(effect, spec.fn));
    } catch (e) {
      effect.status = EffectStatus.FAILED;
      this.audit.updateEffect(effect);
      throw e;
    }
    effect.status = EffectStatus.APPLIED;
    this.audit.updateEffect(effect);
    return effect.result;
  }

  /** Record out-of-band pre-approval for one staged irreversible. The verdict
   *  is recorded, not generated — Pherix never decides for itself. */
  approveIrreversible(effectId: string): void {
    this.guardOpen();
    if (!this.txn.effects.some((e) => e.effectId === effectId)) {
      throw new Error(
        `no staged effect with effectId ${JSON.stringify(effectId)} in ` +
          `transaction ${this.txn.txnId}`,
      );
    }
    this.approvals.add(effectId);
  }

  // --- commit (forward fold) ---

  async commit(): Promise<void> {
    this.guardOpen();

    const staged = this.txn.effects.filter(
      (e) => e.status === EffectStatus.STAGED && !e.reversible,
    );

    if (staged.length > 0) {
      this.txn.transition(TxnState.STAGED);
      this.audit.updateTransactionState(this.txn.txnId, this.txn.state);
    }

    // Commit-time policy re-eval (TOCTOU): walks the entire journal.
    try {
      await this.policy.evaluateJournal(this.txn, this.policyCtx);
    } catch (e) {
      if (e instanceof PolicyViolation) {
        if (e.effectIndex !== null) {
          const denied = this.txn.effects[e.effectIndex];
          if (denied !== undefined) {
            denied.status = EffectStatus.GATED;
            this.audit.updateEffect(denied);
          }
        }
        if (this.txn.state === TxnState.STAGED) {
          await this.partialUnwind();
        } else {
          await this.rollback();
        }
      }
      throw e;
    }

    if (staged.length > 0) {
      // Gate: every staged irreversible needs a compensator OR an approval.
      const needsApproval = staged
        .filter((e) => e.compensator === null && !this.approvals.has(e.effectId))
        .map((e) => e.effectId);
      if (needsApproval.length > 0) {
        for (const e of staged) {
          if (e.compensator === null && !this.approvals.has(e.effectId)) {
            e.status = EffectStatus.GATED;
            this.audit.updateEffect(e);
          }
        }
        await this.partialUnwind();
        throw new GateBlocked(needsApproval);
      }

      // Forward fold over staged irreversibles in journal order. Each fire is
      // awaited: an async irreversible (e.g. a real HTTP POST) fully resolves
      // before the next fires, and a mid-fold rejection drives the mixed-fold
      // unwind rather than escaping as an unhandled rejection.
      for (const e of staged) {
        if (e.status === EffectStatus.APPLIED) {
          // Idempotency: re-fire of an already-applied effect is a no-op.
          continue;
        }
        const adapter = this.resolveAdapter(e.resource);
        const spec = REGISTRY.get(e.tool);
        try {
          e.result = await activeEffect.run(e, async () => adapter.apply(e, spec.fn));
        } catch (err) {
          e.status = EffectStatus.FAILED;
          this.audit.updateEffect(e);
          await this.partialUnwind();
          throw err;
        }
        e.status = EffectStatus.APPLIED;
        this.audit.updateEffect(e);
      }
    }

    // Finalize: commit transactional adapters.
    for (const adapter of uniqueAdapters(this.adapters)) {
      if (isTransactionalAdapter(adapter)) await adapter.commit();
    }
    this.txn.transition(TxnState.COMMITTED);
    this.audit.updateTransactionState(this.txn.txnId, this.txn.state);
    this.finishedFlag = true;
  }

  // --- rollback (backward fold) ---

  async rollback(): Promise<void> {
    this.guardOpen();

    for (let i = this.txn.effects.length - 1; i >= 0; i--) {
      const effect = this.txn.effects[i]!;
      if (effect.snapshot === null) continue; // staged irreversibles never fired
      const adapter = this.resolveAdapter(effect.resource);
      await adapter.restore(effect.snapshot);
      if (effect.status === EffectStatus.APPLIED) {
        effect.status = EffectStatus.COMPENSATED;
        this.audit.updateEffect(effect);
      }
    }

    for (const adapter of uniqueAdapters(this.adapters)) {
      if (isTransactionalAdapter(adapter)) await adapter.rollback();
    }

    this.txn.transition(TxnState.ROLLED_BACK);
    this.audit.updateTransactionState(this.txn.txnId, this.txn.state);
    this.finishedFlag = true;
  }

  // --- dry-run finalise -----------------------------------------------------

  /**
   * Commit-time bracket for a dry-run: capture verdicts, build the result, then
   * unwind everything via the existing rollback bracket. A dry-run is the same
   * forward fold of the journal as a real txn, ending in `rollback` instead of
   * `commit` — the measurement without collapse. What you observe is the
   * journal-as-built plus the policy verdicts that would fire; what survives is
   * nothing (the world is bit-identical to its pre-dry-run state, save for the
   * populated `result` and the audit row's dry_run=1 flag).
   */
  async dryRunFinalise(): Promise<void> {
    // collectVerdicts re-walks the journal in capture mode (no short-circuit).
    const commitVerdicts = await this.policy.collectVerdicts(this.txn, this.policyCtx);
    const allVerdicts = [...this.stageVerdicts, ...commitVerdicts];
    const wouldHaveFired = this.txn.effects.filter(
      (e) => !e.reversible && e.status === EffectStatus.STAGED,
    );
    // State diff is computed *before* the rollback, so the live resource still
    // carries the dry-run's writes.
    const stateDiff = await this.computeStateDiff();
    this.result = new DryRunResult({
      txnId: this.txn.txnId,
      journal: [...this.txn.effects],
      wouldHaveFired,
      policyVerdicts: allVerdicts,
      isClean: allVerdicts.every((v) => v.allow),
      stateDiff,
    });
    // Unwind: identical mechanics to a normal rollback.
    await this.rollback();
  }

  /** Per-resource structural delta — current state vs the begin baseline, keyed
   *  by adapter name. Adapters that did not opt into StateDiffable contribute
   *  nothing (their baseline was never captured). */
  private async computeStateDiff(): Promise<Record<string, Record<string, unknown>>> {
    const out: Record<string, Record<string, unknown>> = {};
    for (const [adapter, baseline] of this.stateBaselines) {
      out[adapter.name] = await adapter.stateDiff(baseline);
    }
    return out;
  }

  // --- mixed-fold unwind after a commit-time failure ---

  private async partialUnwind(): Promise<void> {
    this.txn.transition(TxnState.PARTIAL);
    this.audit.updateTransactionState(this.txn.txnId, this.txn.state);

    let stuck = false;
    for (let i = this.txn.effects.length - 1; i >= 0; i--) {
      const effect = this.txn.effects[i]!;
      if (effect.status !== EffectStatus.APPLIED) continue;

      if (effect.reversible) {
        // Restore from snapshot (state rollback).
        const adapter = this.resolveAdapter(effect.resource);
        if (effect.snapshot !== null) await adapter.restore(effect.snapshot);
        effect.status = EffectStatus.COMPENSATED;
        this.audit.updateEffect(effect);
        continue;
      }

      // Irreversible: invoke the compensator (semantic inverse).
      if (effect.compensator === null || !REGISTRY.has(effect.compensator)) {
        stuck = true;
        continue;
      }
      const compSpec = REGISTRY.get(effect.compensator);
      const compAdapter = this.resolveAdapter(compSpec.resource);
      const compEffect = new Effect({
        txnId: this.txn.txnId,
        index: -1, // synthetic; not persisted
        tool: effect.compensator,
        args: effect.args, // re-invoke with the original args
        resource: compSpec.resource,
        reversible: false,
        effectId: `comp-${effect.effectId}`,
      });
      try {
        await compAdapter.apply(compEffect, compSpec.fn);
      } catch {
        stuck = true;
        continue;
      }
      effect.status = EffectStatus.COMPENSATED;
      this.audit.updateEffect(effect);
    }

    for (const adapter of uniqueAdapters(this.adapters)) {
      if (isTransactionalAdapter(adapter)) await adapter.rollback();
    }

    this.txn.transition(stuck ? TxnState.STUCK : TxnState.ROLLED_BACK);
    this.audit.updateTransactionState(this.txn.txnId, this.txn.state);
    this.finishedFlag = true;
  }
}

export interface AgentTxnOptions extends TxnContextOptions {
  policy?: Policy;
  audit?: AuditJournal;
}

/**
 * Wrap an agent's tool-call layer in a transaction.
 *
 * The body `fn` runs with `ctx` as the active transaction; registered tools
 * called inside it are journalled and routed. On a clean exit the transaction
 * auto-commits; on a thrown exception it auto-rolls-back and the exception
 * propagates. The body may be sync or async (the async-local store survives
 * `await` boundaries). Returns the TxnContext so the caller can inspect final
 * state and the audit journal.
 */
export async function agentTxn(
  adapters: Record<string, ResourceAdapter>,
  fn: (ctx: TxnContext) => unknown | Promise<unknown>,
  options: AgentTxnOptions = {},
): Promise<TxnContext> {
  const policy = options.policy ?? Policy.allowAll();
  const audit = options.audit ?? AuditJournal.inMemory();

  for (const adapter of uniqueAdapters(adapters)) {
    if (isTransactionalAdapter(adapter)) await (adapter as TransactionalResourceAdapter).begin();
  }

  const ctx = new TxnContext(adapters, policy, audit, { clientId: options.clientId ?? null });

  return activeTxn.run(ctx, async () => {
    try {
      await fn(ctx);
      if (!ctx.finished) await ctx.commit();
    } catch (e) {
      if (!ctx.finished) await ctx.rollback();
      throw e;
    }
    return ctx;
  });
}
