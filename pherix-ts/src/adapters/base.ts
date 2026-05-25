/**
 * The ResourceAdapter protocol — the abstraction that makes Pherix a system and
 * not a logging decorator. Mirror of pherix/core/adapters/base.py.
 *
 * An adapter is a triple (snapshot, apply, restore) over a class of real
 * resource. `supportsRollback()` is the honesty flag: when it returns false the
 * runtime forces the effect down the irreversible (staged + gated) lane rather
 * than pretending it can undo what it cannot.
 *
 * Every lifecycle method may return synchronously or as a `Promise` — the
 * runtime `await`s each one. A synchronous driver (better-sqlite3) returns
 * plain values and the `await` is a no-op; an async driver (node-postgres,
 * whose query API has no synchronous form) returns promises. This is the
 * faithful generalisation the async TS ecosystem forces: the substrate gets
 * *more general*, not branched per-driver. Python's lanes are synchronous
 * because psycopg can run a query synchronously; in TS that exception does not
 * hold, so the lane is awaitable here.
 */

import type { Effect, SnapshotHandle } from "../effects.js";

/** A tool implementation. The adapter decides whether to inject a handle. */
export type ToolFn = (...callArgs: any[]) => unknown;

export interface ResourceAdapter {
  readonly name: string;
  /** Honesty flag: can this resource actually be restored? */
  supportsRollback(): boolean;
  /** Capture before-state prior to applying the effect (reversible lane only). */
  snapshot(effect: Effect): SnapshotHandle | Promise<SnapshotHandle>;
  /** Execute the effect by invoking the tool implementation. */
  apply(effect: Effect, toolFn: ToolFn): unknown;
  /** Restore the resource to the state captured by the handle. */
  restore(handle: SnapshotHandle): void | Promise<void>;
}

/**
 * Adapters with a txn-scope lifecycle. `begin()` is called at txn start,
 * `commit()` after all effects fire, `rollback()` on rollback or gate-block.
 * The runtime detects support structurally via `isTransactionalAdapter`.
 */
export interface TransactionalResourceAdapter extends ResourceAdapter {
  begin(): void | Promise<void>;
  commit(): void | Promise<void>;
  rollback(): void | Promise<void>;
}

/**
 * Adapters that can produce a structural state diff for dry-run preview.
 * Opt-in sub-protocol: `stateBaseline()` is captured once at txn begin (dry-run
 * only), `stateDiff(baseline)` is called at the dry-run finalise hook before
 * the rollback discards the world. Detected structurally via
 * `isStateDiffable`. Adapters that do not implement it contribute nothing to
 * the dry-run's `stateDiff` (the irreversible HTTP adapter's structural record
 * is `wouldHaveFired` instead). Both may be sync or async.
 */
export interface StateDiffable extends ResourceAdapter {
  stateBaseline(): unknown | Promise<unknown>;
  stateDiff(baseline: unknown): Record<string, unknown> | Promise<Record<string, unknown>>;
}

export function isStateDiffable(adapter: ResourceAdapter): adapter is StateDiffable {
  const a = adapter as Partial<StateDiffable>;
  return typeof a.stateBaseline === "function" && typeof a.stateDiff === "function";
}

export function isTransactionalAdapter(
  adapter: ResourceAdapter,
): adapter is TransactionalResourceAdapter {
  const a = adapter as Partial<TransactionalResourceAdapter>;
  return (
    typeof a.begin === "function" &&
    typeof a.commit === "function" &&
    typeof a.rollback === "function"
  );
}
