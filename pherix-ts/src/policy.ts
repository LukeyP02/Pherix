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

/**
 * A read mediator answers "what is the live value at (resource, key) right
 * now?" for world-state-aware rules (#7). The runtime owns the adapter map and
 * the live connections, so it constructs one of these and threads it into every
 * PolicyContext. Kept as a bare callable so the runtime can supply a closure
 * over its adapters with zero ceremony, and a test can supply a one-line fake
 * to prove a rule's behaviour at the policy layer.
 */
export type ReadMediator = (resource: string, key: unknown) => unknown | Promise<unknown>;

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

/**
 * A rule is a predicate over (effect, world). It may be synchronous (the
 * common args-only / cap case) or asynchronous — a world-state-aware rule that
 * `await`s `ctx.read` over an async driver (Postgres) returns a `Promise`. The
 * engine awaits every rule uniformly, so a sync rule is unaffected (awaiting a
 * non-promise is a no-op). This is the same widening the adapter lifecycle took
 * for async drivers, applied to the policy axis.
 */
export type RuleFn = (effect: Effect, ctx: PolicyContext) => Verdict | Promise<Verdict>;

/** Anything the engine treats as a rule carries a stable `name` and evaluates. */
export interface NamedRule {
  readonly name: string;
  appliesTo(effect: Effect): boolean;
  contribution(effect: Effect): number;
  evaluate(effect: Effect, ctx: PolicyContext): Verdict | Promise<Verdict>;
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

  evaluate(effect: Effect, ctx: PolicyContext): Verdict | Promise<Verdict> {
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
  private readonly reader: ReadMediator | null;

  constructor(opts: { journal: readonly Effect[]; where: Where; reader?: ReadMediator | null }) {
    this.journalRef = opts.journal;
    this.where = opts.where;
    this.reader = opts.reader ?? null;
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

  /**
   * Read live world-state through the runtime-supplied mediator (#7).
   *
   * A rule that needs live adapter state — e.g. "refund order 42 only if its
   * status is 'paid' *right now*" — calls `ctx.read(resource, key)`. The read
   * is live, so the commit-time re-walk can *diverge* from stage-time: if the
   * world moved between the two evaluations of the same predicate, `ctx.read`
   * returns the new value and the verdict can flip. That divergence is the
   * TOCTOU protection the twice-evaluated bracket exists to provide.
   *
   * Throws if no reader is bound — a rule that needs world state must be run by
   * a runtime (or test) that supplied one. A silent `null` would let a
   * refund-if-paid rule pass against a phantom reading; the loud error is the
   * honest failure mode.
   */
  async read(resource: string, key: unknown): Promise<unknown> {
    if (this.reader === null) {
      throw new Error(
        "PolicyContext.read called but no read mediator is bound. World-state-aware " +
          "rules require the runtime (or test) to construct PolicyContext with a " +
          "reader; the runtime threads its adapter map in as that callable.",
      );
    }
    // Awaitable so a rule reads uniformly whether the underlying driver is
    // synchronous (SQLite) or asynchronous (Postgres). A rule does `await
    // ctx.read(...)` either way.
    return await this.reader(resource, key);
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
  async evaluate(effect: Effect, ctx: PolicyContext, where?: Where): Promise<void> {
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

    // 2. registered rules — awaited so a world-state-aware rule can read live
    // state over an async driver; a sync rule resolves immediately.
    for (const rule of this.rules) {
      const verdict = await rule.evaluate(effect, ctx);
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
      const verdict = await cap.evaluate(effect, ctx);
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
  async evaluateJournal(txn: { effects: Effect[] }, ctx: PolicyContext): Promise<void> {
    ctx.resetCaps();
    for (const effect of txn.effects) {
      await this.evaluate(effect, ctx, "commit");
    }
  }

  // -- capture-mode (dry-run): never raise, return verdicts ------------------

  /**
   * Capture-mode counterpart of `evaluate`. Walks every rule and every cap
   * against `effect`; never raises; returns one PolicyVerdict per rule/cap
   * evaluation. The allow/deny tool-name lists contribute at most one extra
   * verdict, and only on Deny (an allow-list pass is implicit, matching
   * `evaluate`). Caps accumulate only on Allow, so the running total at the end
   * of the walk is identical to what `evaluate` would produce for the same
   * prefix of Allow-yielding effects — the load-bearing equality between
   * raise-mode and capture-mode.
   */
  async tryEvaluate(effect: Effect, ctx: PolicyContext, where?: Where): Promise<PolicyVerdict[]> {
    if (where !== undefined) ctx.where = where;
    const activeWhere = ctx.where;
    const verdicts: PolicyVerdict[] = [];

    // 1. allow/deny — capture as Deny verdict when it bites; passes are implicit.
    if (this.deny.has(effect.tool)) {
      verdicts.push(
        new PolicyVerdict({
          allow: false,
          rule: null,
          effectIndex: effect.index,
          where: activeWhere,
          tool: effect.tool,
          reason: "tool is deny-listed",
        }),
      );
    } else if (this.allow !== null && !this.allow.has(effect.tool)) {
      verdicts.push(
        new PolicyVerdict({
          allow: false,
          rule: null,
          effectIndex: effect.index,
          where: activeWhere,
          tool: effect.tool,
          reason: "tool is not in the allow-list",
        }),
      );
    }

    // 2. registered rules — one verdict each, regardless of outcome.
    for (const rule of this.rules) {
      const v = await rule.evaluate(effect, ctx);
      verdicts.push(
        new PolicyVerdict({
          allow: v.allow,
          rule,
          effectIndex: effect.index,
          where: activeWhere,
          tool: effect.tool,
          reason: v.allow ? null : v.reason,
        }),
      );
    }

    // 3. caps — one verdict each; accumulate only on Allow.
    for (const cap of this.caps) {
      const v = await cap.evaluate(effect, ctx);
      verdicts.push(
        new PolicyVerdict({
          allow: v.allow,
          rule: cap,
          effectIndex: effect.index,
          where: activeWhere,
          tool: effect.tool,
          reason: v.allow ? null : v.reason,
        }),
      );
      if (v.allow && cap.appliesTo(effect)) {
        ctx.capAdd(cap, cap.contribution(effect));
      }
    }

    return verdicts;
  }

  /**
   * Commit-time capture walk over the whole journal. Resets per-cap totals
   * (matching `evaluateJournal`'s re-accumulate-from-zero) then folds forward
   * through every effect with `tryEvaluate`. Never raises on Deny. Used by
   * `dryRun` as the commit-time policy bracket.
   */
  async collectVerdicts(txn: { effects: Effect[] }, ctx: PolicyContext): Promise<PolicyVerdict[]> {
    ctx.resetCaps();
    const out: PolicyVerdict[] = [];
    for (const effect of txn.effects) {
      out.push(...(await this.tryEvaluate(effect, ctx, "commit")));
    }
    return out;
  }
}

// -- capture-mode verdict carrier ------------------------------------------

/**
 * One evaluation of one rule (or cap, or the allow/deny list) against one
 * effect, captured rather than raised. Emitted by `Policy.tryEvaluate`
 * (per stage-time tool call) and `Policy.collectVerdicts` (per commit-time
 * journal walk). Aggregated into `DryRunResult.policyVerdicts`.
 *
 * `rule` is the live rule object, or null for verdicts attributable to the
 * allow/deny tool-name lists. `ruleName` is the convenience handle for
 * printing / asserting.
 */
export class PolicyVerdict {
  readonly allow: boolean;
  readonly rule: NamedRule | null;
  readonly effectIndex: number;
  readonly where: Where;
  readonly tool: string;
  readonly reason: string | null;

  constructor(opts: {
    allow: boolean;
    rule: NamedRule | null;
    effectIndex: number;
    where: Where;
    tool: string;
    reason?: string | null;
  }) {
    this.allow = opts.allow;
    this.rule = opts.rule;
    this.effectIndex = opts.effectIndex;
    this.where = opts.where;
    this.tool = opts.tool;
    this.reason = opts.reason ?? null;
  }

  get ruleName(): string | null {
    return this.rule === null ? null : this.rule.name;
  }
}

// -- #7: world-state-aware rules + the SQL read mediator -------------------

/** The synchronous slice of a SQL connection a reader needs: prepare a query
 *  and fetch one row keyed by a bound parameter. better-sqlite3's `get`
 *  returns the row as an object keyed by column name. */
interface SyncSqlConnection {
  prepare(source: string): { get(...params: unknown[]): unknown };
}

/** The asynchronous slice of a SQL connection a reader needs: node-postgres'
 *  `query(text, params)` returning `{ rows }` keyed by column name. */
interface AsyncSqlConnection {
  query(text: string, params?: unknown[]): Promise<{ rows: Array<Record<string, unknown>> }>;
}

/**
 * Build a ReadMediator over an adapter map for SQL reads. The returned callable
 * takes `(resource, key)` where `key` is a `[table, pkColumn, pkValue,
 * valueColumn]` tuple, and returns the live value of `valueColumn` for that row
 * — or `null` if the row is absent.
 *
 * The read goes through the adapter's own connection, so it sees the
 * transaction's view of committed state at the instant of the call. Identifier
 * parts (table, pkColumn, valueColumn) come from the rule definition — never
 * from agent input — so interpolating them is safe by construction (SQLite
 * cannot parameterise identifiers regardless); pkValue is always bound.
 *
 * Works over both drivers. A synchronous connection (SQLite's `.prepare().get`)
 * returns the value directly; an asynchronous one (Postgres' `.query`) returns
 * a Promise. `ctx.read` awaits whichever it gets, so a world-state rule reads
 * uniformly regardless of the backing database.
 */
export function sqlReader(adapters: Record<string, unknown>): ReadMediator {
  return (resource: string, key: unknown): unknown | Promise<unknown> => {
    const adapter = adapters[resource] as { connection?: unknown } | undefined;
    if (adapter === undefined) {
      throw new Error(`ctx.read: no adapter registered for resource ${JSON.stringify(resource)}`);
    }
    const conn = adapter.connection as
      | (Partial<SyncSqlConnection> & Partial<AsyncSqlConnection>)
      | undefined;
    const [table, pkCol, pkVal, valueCol] = key as [string, string, unknown, string];

    // Synchronous driver (SQLite): prepare + get, return the value directly.
    if (conn !== undefined && typeof conn.prepare === "function") {
      const row = conn.prepare(`SELECT ${valueCol} FROM ${table} WHERE ${pkCol} = ?`).get(pkVal) as
        | Record<string, unknown>
        | undefined;
      return row === undefined ? null : row[valueCol];
    }

    // Asynchronous driver (Postgres): query with a bound $1, await the rows.
    if (conn !== undefined && typeof conn.query === "function") {
      return conn
        .query(`SELECT ${valueCol} FROM ${table} WHERE ${pkCol} = $1`, [pkVal])
        .then((res) => (res.rows.length === 0 ? null : (res.rows[0]![valueCol] ?? null)));
    }

    throw new Error(
      `ctx.read: adapter for resource ${JSON.stringify(resource)} has no SQL connection ` +
        `(.connection.prepare for SQLite or .connection.query for Postgres); world-state ` +
        `reads need a SQL-shaped adapter`,
    );
  };
}

export interface RefundIfPaidOptions {
  tool?: string;
  table?: string;
  idArg?: string;
  pkColumn?: string;
  statusColumn?: string;
  paidValue?: string;
  resource?: string;
}

/**
 * The canonical #7 rule: refund order N only if it is 'paid' *right now*.
 *
 * Returns a rule `(effect, ctx) -> Verdict` suitable for `policy.rule(...)`. It
 * applies only to `tool` calls; for every other tool it is a no-op Allow.
 *
 * The rule reads the order's live status via `ctx.read` at evaluation time.
 * Because the runtime evaluates the policy twice — stage-time and commit-time —
 * the same predicate is checked against the world as it stands at each moment.
 * If the order is 'paid' when staged but a concurrent actor flips it before
 * commit, the stage-time read returns 'paid' (Allow) and the commit-time read
 * returns the new value (Deny). That divergence is the TOCTOU protection an
 * args-only rule cannot provide — the args never changed, only the world did.
 */
export function refundIfPaid(opts: RefundIfPaidOptions = {}): RuleFn {
  const tool = opts.tool ?? "refundOrder";
  const table = opts.table ?? "orders";
  const idArg = opts.idArg ?? "orderId";
  const pkColumn = opts.pkColumn ?? "id";
  const statusColumn = opts.statusColumn ?? "status";
  const paidValue = opts.paidValue ?? "paid";
  const resource = opts.resource ?? "sql";

  const rule: RuleFn = async (effect, ctx) => {
    if (effect.tool !== tool) return Allow();
    if (!(idArg in effect.args)) {
      return Deny(`refundIfPaid: tool ${JSON.stringify(tool)} has no ${JSON.stringify(idArg)} arg`);
    }
    const orderId = effect.args[idArg];
    const liveStatus = await ctx.read(resource, [table, pkColumn, orderId, statusColumn]);
    if (liveStatus !== paidValue) {
      return Deny(
        `refundIfPaid: order ${JSON.stringify(orderId)} is ${JSON.stringify(liveStatus)}, not ` +
          `${JSON.stringify(paidValue)} — refusing to refund a non-paid order (checked live at ` +
          `${ctx.where}-time)`,
      );
    }
    return Allow();
  };
  Object.defineProperty(rule, "name", { value: `refundIfPaid(${tool})` });
  return rule;
}
