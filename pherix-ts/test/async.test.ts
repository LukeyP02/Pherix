/**
 * Async tools — the normal TS case (HTTP/DB clients return Promises). These
 * tests would fail against a synchronous runtime that assigns the returned
 * Promise as the result and marks APPLIED before it settles: a rejection would
 * escape as an unhandled rejection with no FAILED status and no unwind.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  EffectStatus,
  StagedResult,
  TxnState,
  agentTxn,
  tool,
  type SqliteDatabase,
} from "../src/index.js";
import { setup, type Fixture } from "./helpers.js";

let f: Fixture;
beforeEach(() => {
  f = setup();
});

/** A reversible SQL tool that awaits before writing — async to the core. */
function asyncTransfer() {
  return tool<{ from: string; to: string; amount: number }>(
    "sql",
    async (conn: SqliteDatabase, args) => {
      await Promise.resolve(); // a real async hop (await a driver round-trip)
      conn.prepare("UPDATE accounts SET balance = balance - ? WHERE name = ?").run(args.amount, args.from);
      conn.prepare("UPDATE accounts SET balance = balance + ? WHERE name = ?").run(args.amount, args.to);
      return { ok: true };
    },
    { name: "asyncTransfer" },
  );
}

describe("async tools", () => {
  it("a resolved async reversible commits with the result recorded", async () => {
    const xfer = asyncTransfer();
    const ctx = await agentTxn(f.adapters, async () => {
      const r = await xfer({ from: "alice", to: "bob", amount: 25 });
      expect(r).toEqual({ ok: true });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(ctx.txn.effects[0]!.status).toBe(EffectStatus.APPLIED);
    expect(f.balanceOf("alice")).toBe(75);
    expect(f.balanceOf("bob")).toBe(25);
  });

  it("a rejected async reversible drives FAILED + rollback (rejection does not escape)", async () => {
    const rejecter = tool(
      "sql",
      async (_conn: SqliteDatabase, _args: Record<string, unknown>) => {
        await Promise.resolve();
        throw new Error("async apply failed");
      },
      { name: "rejecter" },
    );
    const xfer = asyncTransfer();
    let ctx: { txn: { state: TxnState; effects: { status: EffectStatus }[] } } | undefined;
    await expect(
      agentTxn(f.adapters, async (txn) => {
        ctx = txn;
        await xfer({ from: "alice", to: "bob", amount: 25 });
        await rejecter({});
      }),
    ).rejects.toThrow("async apply failed");
    // The failing effect is FAILED, the txn rolled back, the world is untouched.
    expect(ctx!.txn.state).toBe(TxnState.ROLLED_BACK);
    expect(ctx!.txn.effects[1]!.status).toBe(EffectStatus.FAILED);
    expect(f.balanceOf("alice")).toBe(100);
    expect(f.balanceOf("bob")).toBe(0);
  });

  it("a rejected async irreversible at commit-fire drives the mixed-fold unwind", async () => {
    // charge fires first (compensator refund), then asyncFail rejects when it
    // fires at commit. The rejection is caught (not escaped) and drives the
    // mixed-fold unwind: charge is compensated by an awaited refund. asyncFail
    // is FAILED (never APPLIED), so it is not itself compensated and the txn
    // unwinds cleanly to ROLLED_BACK.
    const asyncFail = tool(
      "http",
      async (_args: Record<string, unknown>) => {
        await Promise.resolve();
        throw new Error("downstream timeout");
      },
      { injectsHandle: false, name: "asyncFail" },
    );
    let ctx: { txn: { state: TxnState; effects: { tool: string; status: EffectStatus }[] } } | undefined;
    await expect(
      agentTxn(f.adapters, async (txn) => {
        ctx = txn;
        await f.tools.charge({ card: "A", amount: 10 }); // compensator refund
        const s = (await asyncFail({})) as StagedResult;
        txn.approveIrreversible(s.effectId);
      }),
    ).rejects.toThrow("downstream timeout");
    expect(ctx!.txn.state).toBe(TxnState.ROLLED_BACK);
    // charge was compensated by an awaited refund during the unwind...
    expect(f.log.sent.map((s) => s.tool)).toEqual(["charge", "refund"]);
    // ...and the rejected fire is recorded FAILED, not silently dropped.
    expect(ctx!.txn.effects.find((e) => e.tool === "asyncFail")!.status).toBe(EffectStatus.FAILED);
  });
});
