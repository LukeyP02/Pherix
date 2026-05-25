/** Mirrors tests/test_transaction.py — the state-machine invariants. */

import { describe, expect, it } from "vitest";
import { Effect, Transaction, TransactionStateError, TxnState } from "../src/index.js";

function mkEffect(txn: Transaction): Effect {
  return new Effect({
    txnId: txn.txnId,
    index: txn.nextIndex(),
    tool: "t",
    args: {},
    resource: "sql",
    reversible: true,
  });
}

describe("Transaction", () => {
  it("defaults to OPEN with an empty journal", () => {
    const t = new Transaction();
    expect(t.state).toBe(TxnState.OPEN);
    expect(t.effects).toEqual([]);
    expect(t.isOpen).toBe(true);
  });

  it("generates unique txn ids", () => {
    expect(new Transaction().txnId).not.toBe(new Transaction().txnId);
  });

  it("appends to the journal and tracks nextIndex", () => {
    const t = new Transaction();
    expect(t.nextIndex()).toBe(0);
    t.addEffect(mkEffect(t));
    expect(t.nextIndex()).toBe(1);
    expect(t.effects).toHaveLength(1);
  });

  it("allows OPEN -> COMMITTED and OPEN -> ROLLED_BACK", () => {
    const a = new Transaction();
    a.transition(TxnState.COMMITTED);
    expect(a.state).toBe(TxnState.COMMITTED);
    const b = new Transaction();
    b.transition(TxnState.ROLLED_BACK);
    expect(b.state).toBe(TxnState.ROLLED_BACK);
  });

  it("rejects a double commit", () => {
    const t = new Transaction();
    t.transition(TxnState.COMMITTED);
    expect(() => t.transition(TxnState.COMMITTED)).toThrow(TransactionStateError);
  });

  it("rejects commit after rollback", () => {
    const t = new Transaction();
    t.transition(TxnState.ROLLED_BACK);
    expect(() => t.transition(TxnState.COMMITTED)).toThrow(TransactionStateError);
  });

  it("closes the journal after a terminal transition", () => {
    const t = new Transaction();
    t.transition(TxnState.COMMITTED);
    expect(() => t.addEffect(mkEffect(t))).toThrow(TransactionStateError);
  });

  it("walks the staged-irreversible path OPEN -> STAGED -> COMMITTED", () => {
    const t = new Transaction();
    t.transition(TxnState.STAGED);
    t.transition(TxnState.COMMITTED);
    expect(t.state).toBe(TxnState.COMMITTED);
  });

  it("walks the partial-commit recovery path STAGED -> PARTIAL -> STUCK", () => {
    const t = new Transaction();
    t.transition(TxnState.STAGED);
    t.transition(TxnState.PARTIAL);
    t.transition(TxnState.STUCK);
    expect(t.state).toBe(TxnState.STUCK);
    // STUCK is terminal.
    expect(() => t.transition(TxnState.ROLLED_BACK)).toThrow(TransactionStateError);
  });

  it("rejects OPEN -> PARTIAL (only reachable via STAGED)", () => {
    const t = new Transaction();
    expect(() => t.transition(TxnState.PARTIAL)).toThrow(TransactionStateError);
  });
});
