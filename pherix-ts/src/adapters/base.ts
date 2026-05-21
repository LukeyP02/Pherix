/**
 * The ResourceAdapter protocol — the abstraction that makes Pherix a system and
 * not a logging decorator. Mirror of pherix/core/adapters/base.py.
 *
 * An adapter is a triple (snapshot, apply, restore) over a class of real
 * resource. `supportsRollback()` is the honesty flag: when it returns false the
 * runtime forces the effect down the irreversible (staged + gated) lane rather
 * than pretending it can undo what it cannot.
 */

import type { Effect, SnapshotHandle } from "../effects.js";

/** A tool implementation. The adapter decides whether to inject a handle. */
export type ToolFn = (...callArgs: any[]) => unknown;

export interface ResourceAdapter {
  readonly name: string;
  /** Honesty flag: can this resource actually be restored? */
  supportsRollback(): boolean;
  /** Capture before-state prior to applying the effect (reversible lane only). */
  snapshot(effect: Effect): SnapshotHandle;
  /** Execute the effect by invoking the tool implementation. */
  apply(effect: Effect, toolFn: ToolFn): unknown;
  /** Restore the resource to the state captured by the handle. */
  restore(handle: SnapshotHandle): void;
}

/**
 * Adapters with a txn-scope lifecycle. `begin()` is called at txn start,
 * `commit()` after all effects fire, `rollback()` on rollback or gate-block.
 * The runtime detects support structurally via `isTransactionalAdapter`.
 */
export interface TransactionalResourceAdapter extends ResourceAdapter {
  begin(): void;
  commit(): void;
  rollback(): void;
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
