/**
 * Capability policy — a predicate fold over the journal.
 * Mirror of pherix/core/policy.py.
 *
 * A rule is a callable `(effect, ctx) -> Allow | Deny(reason)`. The fold runs
 * twice: stage-time (when the runtime intercepts a tool call, before the effect
 * is journalled — cheap, fails fast) and commit-time (after every effect is in
 * the journal, just before the adapter commit bracket — re-walks and
 * re-evaluates every rule against every effect). This twice-evaluation is the
 * TOCTOU safety property: state may have changed between the two walks.
 *
 * Caps (`Cap.count` / `Cap.sum`) are rules whose context-carried running total
 * turns the predicate from "this single effect" into "this effect against the
 * journal so far". Deny always wins; rules and caps fire in registration order;
 * the first Deny short-circuits with a PolicyViolation.
 */

import type { Effect } from "./effects.js";

export type Where = "stage" | "commit";

export class PolicyViolation extends Error {
  tool: string | null;
  reason: string;
  where: Where;
  rule: NamedRule | null;
  effectIndex: number | null;

  constructor(
    reason: string,
    opts: {
      tool?: string | null;
      where?: Where;
      rule?: NamedRule | null;
      effectIndex?: number | null;
    } = {},
  ) {
    const tool = opts.tool ?? null;
    const where = opts.where ?? "stage";
    const rule = opts.rule ?? null;
    let msg = "policy denied";
    if (tool !== null) msg += ` tool ${JSON.stringify(tool)}`;
    msg += `: ${reason}`;
    if (rule !== null) msg += ` (rule=${rule.name}, where=${where})`;
    super(msg);
    this.name = "PolicyViolation";
    this.tool = tool;
    this.reason = reason;
    this.where = where;
    this.rule = rule;
    this.effectIndex = opts.effectIndex ?? null;
  }
}

// -- verdicts ----------------------------------------------------------------

export type Verdict = { allow: true } | { allow: false; reason: string };

export function Allow(): Verdict {
  return { allow: true };
}

export function Deny(reason: string): Verdict {
  return { allow: false, reason };
}

// -- rules -------------------------------------------------------------------

export type RuleFn = (effect: Effect, ctx: PolicyContext) => Verdict;

/** Anything the engine treats as a rule carries a stable `name` and evaluates. */
export interface NamedRule {
  readonly name: string;
  appliesTo(effect: Effect): boolean;
  contribution(effect: Effect): number;
  evaluate(effect: Effect, ctx: PolicyContext): Verdict;
}

export class PolicyRule implements NamedRule {
  readonly name: string;
  private readonly fn: RuleFn;

  constructor(fn: RuleFn, name?: string) {
    this.fn = fn;
    this.name = name && name.length > 0 ? name : fn.name || "<rule>";
  }

  appliesTo(_effect: Effect): boolean {
    return true;
  }

  contribution(_effect: Effect): number {
    return 0;
  }

  evaluate(effect: Effect, ctx: PolicyContext): Verdict {
    return this.fn(effect, ctx);
  }
}

// -- spend caps --------------------------------------------------------------

class CountCap implements NamedRule {
  constructor(
    readonly tool: string,
    readonly max: number,
  ) {}

  get name(): string {
    return `Cap.count(tool=${JSON.stringify(this.tool)}, max=${this.max})`;
  }

  appliesTo(effect: Effect): boolean {
    return effect.tool === this.tool;
  }

  contribution(_effect: Effect): number {
    return 1;
  }

  evaluate(effect: Effect, ctx: PolicyContext): Verdict {
    if (!this.appliesTo(effect)) return Allow();
    const running = ctx.capRunning(this);
    if (running + 1 > this.max) {
      return Deny(
        `would exceed count cap (max=${this.max}) for tool ` +
          `${JSON.stringify(this.tool)}; already at ${running}`,
      );
    }
    return Allow();
  }
}

class SumCap implements NamedRule {
  constructor(
    readonly tool: string,
    readonly via: (args: Record<string, unknown>) => number,
    readonly max: number,
  ) {}

  get name(): string {
    return `Cap.sum(tool=${JSON.stringify(this.tool)}, max=${this.max})`;
  }

  appliesTo(effect: Effect): boolean {
    return effect.tool === this.tool;
  }

  contribution(effect: Effect): number {
    return this.via(effect.args);
  }

  evaluate(effect: Effect, ctx: PolicyContext): Verdict {
    if (!this.appliesTo(effect)) return Allow();
    const running = ctx.capRunning(this);
    const contribution = this.contribution(effect);
    const candidate = running + contribution;
    if (candidate > this.max) {
      return Deny(
        `would exceed sum cap (max=${this.max}) for tool ` +
          `${JSON.stringify(this.tool)}; running=${running}, contribution=${contribution}`,
      );
    }
    return Allow();
  }
}

/** Namespace for spend-cap primitives. Caps are themselves rules. */
export const Cap = {
  count(opts: { tool: string; max: number }): NamedRule {
    return new CountCap(opts.tool, opts.max);
  },
  sum(opts: {
    tool: string;
    via: (args: Record<string, unknown>) => number;
    max: number;
  }): NamedRule {
    return new SumCap(opts.tool, opts.via, opts.max);
  },
};

// -- evaluation context ------------------------------------------------------

/**
 * Runtime-owned object passed to every rule evaluation. Carries the journal so
 * far, the per-cap running totals (keyed by object identity, so two
 * structurally identical caps are independent buckets), the `where` label, and
 * the `read` placeholder for world-state-aware rules.
 */
export class PolicyContext {
  private readonly journalRef: readonly Effect[];
  where: Where;
  private readonly capTotals = new Map<NamedRule, number>();

  constructor(opts: { journal: readonly Effect[]; where: Where }) {
    this.journalRef = opts.journal;
    this.where = opts.where;
  }

  /** The journal so far, as a snapshot a rule cannot mutate. */
  get journal(): readonly Effect[] {
    return [...this.journalRef];
  }

  capRunning(cap: NamedRule): number {
    return this.capTotals.get(cap) ?? 0;
  }

  capAdd(cap: NamedRule, value: number): void {
    this.capTotals.set(cap, this.capRunning(cap) + value);
  }

  /** Clear all per-cap totals — used between the stage and commit walks. */
  resetCaps(): void {
    this.capTotals.clear();
  }

  read(_resource: string, _key: unknown): unknown {
    throw new Error("world-state-aware reads are not implemented in the base SDK");
  }
}

// -- the policy itself -------------------------------------------------------

export interface PolicyInit {
  allow?: Iterable<string> | null;
  deny?: Iterable<string>;
  rules?: RuleFn[];
  caps?: NamedRule[];
}

export class Policy {
  allow: Set<string> | null;
  deny: Set<string>;
  rules: PolicyRule[];
  caps: NamedRule[];

  constructor(init: PolicyInit = {}) {
    this.allow = init.allow == null ? null : new Set(init.allow);
    this.deny = new Set(init.deny ?? []);
    this.rules = (init.rules ?? []).map((fn) => new PolicyRule(fn));
    this.caps = [...(init.caps ?? [])];
  }

  static allowAll(): Policy {
    return new Policy();
  }

  /** Compose a policy declaratively. */
  static withRules(init: PolicyInit = {}): Policy {
    return new Policy(init);
  }

  /** Register a rule on this policy. Returns the fn unchanged so it stays
   *  unit-testable outside Pherix. */
  rule(fn: RuleFn, name?: string): RuleFn {
    this.rules.push(new PolicyRule(fn, name));
    return fn;
  }

  addCap(cap: NamedRule): void {
    this.caps.push(cap);
  }

  // -- legacy tool-name entry point -----------------------------------------

  check(tool: string): void {
    if (this.deny.has(tool)) {
      throw new PolicyViolation("tool is deny-listed", { tool });
    }
    if (this.allow !== null && !this.allow.has(tool)) {
      throw new PolicyViolation("tool is not in the allow-list", { tool });
    }
  }

  permits(tool: string): boolean {
    try {
      this.check(tool);
      return true;
    } catch (e) {
      if (e instanceof PolicyViolation) return false;
      throw e;
    }
  }

  // -- the predicate-fold entry point ---------------------------------------

  /**
   * Evaluate every applicable rule against `effect`, folding three layers:
   * allow/deny lists, then rules, then caps (accumulating contribution on
   * Allow). Raises PolicyViolation on the first Deny.
   */
  evaluate(effect: Effect, ctx: PolicyContext, where?: Where): void {
    if (where !== undefined) ctx.where = where;
    const activeWhere = ctx.where;
    const indexFor = (): number | null => (activeWhere === "commit" ? effect.index : null);

    // 1. allow/deny
    if (this.deny.has(effect.tool)) {
      throw new PolicyViolation("tool is deny-listed", {
        tool: effect.tool,
        where: activeWhere,
        effectIndex: indexFor(),
      });
    }
    if (this.allow !== null && !this.allow.has(effect.tool)) {
      throw new PolicyViolation("tool is not in the allow-list", {
        tool: effect.tool,
        where: activeWhere,
        effectIndex: indexFor(),
      });
    }

    // 2. registered rules
    for (const rule of this.rules) {
      const verdict = rule.evaluate(effect, ctx);
      if (!verdict.allow) {
        throw new PolicyViolation(verdict.reason, {
          tool: effect.tool,
          where: activeWhere,
          rule,
          effectIndex: indexFor(),
        });
      }
    }

    // 3. caps — evaluate, then accumulate on Allow.
    for (const cap of this.caps) {
      const verdict = cap.evaluate(effect, ctx);
      if (!verdict.allow) {
        throw new PolicyViolation(verdict.reason, {
          tool: effect.tool,
          where: activeWhere,
          rule: cap,
          effectIndex: indexFor(),
        });
      }
      if (cap.appliesTo(effect)) {
        ctx.capAdd(cap, cap.contribution(effect));
      }
    }
  }

  /**
   * Commit-time re-walk: reset per-cap totals, then re-evaluate every rule
   * against every effect in journal order. First Deny raises with
   * where="commit" and the offending effect's index.
   */
  evaluateJournal(txn: { effects: Effect[] }, ctx: PolicyContext): void {
    ctx.resetCaps();
    for (const effect of txn.effects) {
      this.evaluate(effect, ctx, "commit");
    }
  }
}
