/** Mirrors tests/test_runtime_partial_commit.py — the mixed-fold unwind. */

import { beforeEach, describe, expect, it } from "vitest";
import { EffectStatus, StagedResult, TxnState, agentTxn, tool } from "../src/index.js";
import { setup, type Fixture } from "./helpers.js";

let f: Fixture;
beforeEach(() => {
  f = setup();
});

/** A staged irreversible that fails when it fires at commit. */
function failingSend() {
  return tool("http", (_args: Record<string, unknown>) => {
    throw new Error("downstream 500");
  }, { injectsHandle: false, name: "failSend" });
}

describe("partial-commit (mixed-fold) unwind", () => {
  it("fires compensators in reverse (LIFO) order when a later effect fails", async () => {
    const fail = failingSend();
    let ctx: { txn: { state: TxnState } } | undefined;
    try {
      ctx = await agentTxn(f.adapters, async (txn) => {
        ctx = txn;
        await f.tools.charge({ card: "A", amount: 10 }); // compensator refund
        await f.tools.charge({ card: "B", amount: 20 }); // compensator refund
        const s = (await fail({})) as StagedResult;
        txn.approveIrreversible(s.effectId); // let it through the gate so it can fire + fail
      });
    } catch {
      /* the failing fire propagates */
    }
    // charge A fired, charge B fired, then fail threw -> unwind compensates B then A.
    expect(f.log.sent.map((s) => `${s.tool}:${s.args.card ?? ""}`)).toEqual([
      "charge:A",
      "charge:B",
      "refund:B",
      "refund:A",
    ]);
    expect(ctx!.txn.state).toBe(TxnState.ROLLED_BACK);
  });

  it("lands in STUCK when a fired irreversible has no compensator", async () => {
    const fail = failingSend();
    let ctx: { txn: { state: TxnState; effects: { status: EffectStatus; tool: string }[] } } | undefined;
    try {
      ctx = await agentTxn(f.adapters, async (txn) => {
        ctx = txn;
        const a = (await f.tools.sendEmail({ to: "x@y.z", body: "hi" })) as StagedResult; // no compensator
        txn.approveIrreversible(a.effectId);
        const s = (await fail({})) as StagedResult;
        txn.approveIrreversible(s.effectId);
      });
    } catch {
      /* expected */
    }
    expect(ctx!.txn.state).toBe(TxnState.STUCK);
    // The un-compensatable effect stays APPLIED for operator recovery.
    const email = ctx!.txn.effects.find((e) => e.tool === "sendEmail")!;
    expect(email.status).toBe(EffectStatus.APPLIED);
  });

  it("lands in STUCK when a compensator itself raises", async () => {
    // refundBad throws; chargeBad declares it as its compensator.
    tool("http", (_args: Record<string, unknown>) => {
      throw new Error("refund failed");
    }, { injectsHandle: false, name: "refundBad" });
    const chargeBad = tool("http", (args: Record<string, unknown>) => {
      f.log.sent.push({ tool: "chargeBad", args });
      return { ok: true };
    }, { injectsHandle: false, name: "chargeBad", compensator: "refundBad" });
    const fail = failingSend();
    let ctx: { txn: { state: TxnState } } | undefined;
    try {
      ctx = await agentTxn(f.adapters, async (txn) => {
        ctx = txn;
        await chargeBad({ card: "Z" });
        const s = (await fail({})) as StagedResult;
        txn.approveIrreversible(s.effectId);
      });
    } catch {
      /* expected */
    }
    expect(ctx!.txn.state).toBe(TxnState.STUCK);
  });

  it("restores already-applied reversibles during the same unwind", async () => {
    const fail = failingSend();
    try {
      await agentTxn(f.adapters, async (txn) => {
        await f.tools.transfer({ from: "alice", to: "bob", amount: 40 }); // reversible, applied live
        const s = (await fail({})) as StagedResult;
        txn.approveIrreversible(s.effectId);
      });
    } catch {
      /* expected */
    }
    // The reversible write is restored via snapshot during the mixed-fold unwind.
    expect(f.balanceOf("alice")).toBe(100);
    expect(f.balanceOf("bob")).toBe(0);
  });
});
