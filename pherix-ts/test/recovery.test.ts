/** Mirrors tests/test_recovery.py — crash-consistent recovery (#9).
 *
 * A true crash (process death) can't be staged in-process, so we simulate its
 * aftermath the honest way: write a durable journal left mid-flight (a
 * non-terminal txn with an APPLIED effect still standing), close the handle (the
 * "process died"), then call recover() against the file in a fresh handle — the
 * "new process after the crash" — and assert it resumes the backward fold. */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  AuditJournal,
  Effect,
  EffectStatus,
  HttpAdapter,
  REGISTRY,
  Transaction,
  TxnState,
  recover,
  tool,
} from "../src/index.js";

let dir: string;
let dbPath: string;
let refunded: string[];

beforeEach(() => {
  REGISTRY.clear();
  dir = mkdtempSync(path.join(tmpdir(), "pherix_rec_"));
  dbPath = path.join(dir, "audit.db");
  refunded = [];
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

/** Register charge (irreversible, compensator=refund) and refund. */
function registerChargeRefund() {
  tool<{ idempotencyKey: string; amountCents: number }>(
    "payments",
    (args) => {
      refunded.push(args.idempotencyKey);
      return { refunded: args.idempotencyKey };
    },
    { injectsHandle: false, name: "refund" },
  );
  tool<{ idempotencyKey: string; amountCents: number }>(
    "payments",
    (args) => ({ charged: args.idempotencyKey }),
    { injectsHandle: false, name: "charge", compensator: "refund" },
  );
}

/** Write a durable journal that looks like a crash left it: a `partial` txn with
 *  one APPLIED effect of the given shape. Returns the txn id. */
function writeCrashedTxn(opts: {
  tool: string;
  resource: string;
  reversible: boolean;
  args: Record<string, unknown>;
}): string {
  const audit = new AuditJournal(dbPath);
  const txn = new Transaction();
  audit.recordTransaction(txn);
  const effect = new Effect({
    txnId: txn.txnId,
    index: 0,
    tool: opts.tool,
    args: opts.args,
    resource: opts.resource,
    reversible: opts.reversible,
  });
  effect.status = EffectStatus.APPLIED;
  audit.recordEffect(effect);
  // The crash froze it mid-flight: non-terminal state, applied effect standing.
  audit.updateTransactionState(txn.txnId, TxnState.PARTIAL);
  audit.close(); // the "process died"
  return txn.txnId;
}

describe("recover fires the compensator for a standing irreversible", () => {
  it("re-fires the inverse and lands the txn ROLLED_BACK", async () => {
    registerChargeRefund();
    const txnId = writeCrashedTxn({
      tool: "charge",
      resource: "payments",
      reversible: false,
      args: { idempotencyKey: "k1", amountCents: 500 },
    });

    const report = await recover(dbPath, { payments: new HttpAdapter() });

    expect(refunded).toEqual(["k1"]); // the inverse fired with the journalled args
    expect(report.compensatorsFired).toBe(1);
    expect(report.recovered).toBe(1);
    expect(report.transactions[0]!.finalState).toBe(TxnState.ROLLED_BACK);
    expect(report.transactions[0]!.txnId).toBe(txnId);
  });

  it("is idempotent: a second pass sees COMPENSATED and never re-fires", async () => {
    registerChargeRefund();
    writeCrashedTxn({
      tool: "charge",
      resource: "payments",
      reversible: false,
      args: { idempotencyKey: "k1", amountCents: 500 },
    });

    await recover(dbPath, { payments: new HttpAdapter() });
    expect(refunded).toEqual(["k1"]);

    // Second pass over the same durable journal — the status fence skips it.
    const second = await recover(dbPath, { payments: new HttpAdapter() });
    expect(refunded).toEqual(["k1"]); // NOT fired twice
    // Nothing left mid-flight (the txn is terminal now), so no work to do.
    expect(second.transactions).toHaveLength(0);
  });
});

describe("recover is honest about what it cannot undo", () => {
  it("an irreversible with no registered compensator lands STUCK", async () => {
    // Register only an un-compensated irreversible.
    tool("payments", (args) => args, {
      injectsHandle: false,
      name: "chargeNoComp",
    });
    writeCrashedTxn({
      tool: "chargeNoComp",
      resource: "payments",
      reversible: false,
      args: { amountCents: 100 },
    });

    const report = await recover(dbPath, { payments: new HttpAdapter() });
    expect(report.stuck).toBe(1);
    expect(report.transactions[0]!.finalState).toBe(TxnState.STUCK);
    expect(report.transactions[0]!.effects[0]!.action).toBe("stuck_missing_compensator");
  });

  it("an APPLIED reversible is recorded as DB-auto-rolled-back, no fire", async () => {
    writeCrashedTxn({
      tool: "transfer",
      resource: "sql",
      reversible: true,
      args: { amount: 30 },
    });

    const report = await recover(dbPath, {});
    expect(report.recovered).toBe(1);
    expect(report.transactions[0]!.effects[0]!.action).toBe("db_auto_rolled_back");
    expect(report.transactions[0]!.finalState).toBe(TxnState.ROLLED_BACK);
  });
});
