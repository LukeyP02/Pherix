/**
 * Transaction: the state machine that owns the ordered effect journal —
 * the TypeScript mirror of pherix/core/transaction.py.
 *
 * State transitions:
 * - OPEN -> STAGED      commit() invoked with at least one staged irreversible.
 * - STAGED -> COMMITTED all staged irreversibles fired successfully.
 * - STAGED -> PARTIAL   a staged irreversible failed mid-fire; compensators run.
 * - PARTIAL -> ROLLED_BACK  unwind completed; world is back to pre-txn state.
 * - PARTIAL -> STUCK    a compensator was missing or itself failed; operator
 *                       intervention required (the journal carries recovery info).
 * - OPEN -> ROLLED_BACK explicit rollback before commit; staged effects never
 *                       fired (the strongest containment property).
 * - OPEN -> COMMITTED   the all-reversible / no-staged commit path.
 */

import { randomBytes } from "node:crypto";
import type { Effect } from "./effects.js";

export enum TxnState {
  OPEN = "open",
  STAGED = "staged",
  COMMITTED = "committed",
  ROLLED_BACK = "rolled_back",
  PARTIAL = "partial",
  STUCK = "stuck",
}

/** Raised on an illegal transaction state transition or journal mutation. */
export class TransactionStateError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TransactionStateError";
  }
}

const ALLOWED_TRANSITIONS: Record<string, Set<TxnState>> = {
  [TxnState.OPEN]: new Set([TxnState.STAGED, TxnState.COMMITTED, TxnState.ROLLED_BACK]),
  [TxnState.STAGED]: new Set([TxnState.COMMITTED, TxnState.PARTIAL]),
  [TxnState.PARTIAL]: new Set([TxnState.ROLLED_BACK, TxnState.STUCK]),
};

export function newTxnId(): string {
  return `txn-${randomBytes(6).toString("hex")}`;
}

export class Transaction {
  txnId: string;
  state: TxnState = TxnState.OPEN;
  effects: Effect[] = [];
  policy: unknown = null;
  /** Source txnId when this transaction is itself a replay; null otherwise. */
  replayedFrom: string | null = null;

  constructor(txnId?: string) {
    this.txnId = txnId ?? newTxnId();
  }

  get isOpen(): boolean {
    return this.state === TxnState.OPEN;
  }

  /** Index the next appended effect will occupy. */
  nextIndex(): number {
    return this.effects.length;
  }

  addEffect(effect: Effect): void {
    if (!this.isOpen) {
      throw new TransactionStateError(
        `cannot append to journal of transaction in state ${this.state}`,
      );
    }
    this.effects.push(effect);
  }

  transition(to: TxnState): void {
    const allowed = ALLOWED_TRANSITIONS[this.state] ?? new Set<TxnState>();
    if (!allowed.has(to)) {
      throw new TransactionStateError(`illegal transition ${this.state} -> ${to}`);
    }
    this.state = to;
  }
}
