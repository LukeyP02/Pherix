/** Mirrors tests/test_runtime_gate.py — the human gate + commit-time TOCTOU. */

import { beforeEach, describe, expect, it } from "vitest";
import { Deny, EffectStatus, GateBlocked, Policy, StagedResult, TxnState, agentTxn } from "../src/index.js";
import { setup, type Fixture } from "./helpers.js";

let f: Fixture;
beforeEach(() => {
  f = setup();
});

describe("the gate", () => {
  it("blocks commit when a staged irreversible has neither compensator nor approval", async () => {
    await expect(
      agentTxn(f.adapters, () => {
        f.tools.sendEmail({ to: "x@y.z", body: "hi" });
      }),
    ).rejects.toThrow(GateBlocked);
    expect(f.log.sent).toHaveLength(0);
  });

  it("lists all unapproved effect ids in the GateBlocked error", async () => {
    let err: GateBlocked | undefined;
    try {
      await agentTxn(f.adapters, () => {
        f.tools.sendEmail({ to: "a@y.z", body: "1" });
        f.tools.sendEmail({ to: "b@y.z", body: "2" });
      });
    } catch (e) {
      err = e as GateBlocked;
    }
    expect(err).toBeInstanceOf(GateBlocked);
    expect(err!.needsApproval).toHaveLength(2);
  });

  it("marks gated effects GATED and the txn ROLLED_BACK", async () => {
    let ctx: { txn: { state: TxnState; effects: { status: EffectStatus }[] } } | undefined;
    try {
      ctx = await agentTxn(f.adapters, (txn) => {
        ctx = txn;
        f.tools.sendEmail({ to: "x@y.z", body: "hi" });
      });
    } catch {
      /* GateBlocked expected */
    }
    expect(ctx!.txn.state).toBe(TxnState.ROLLED_BACK);
    expect(ctx!.txn.effects[0]!.status).toBe(EffectStatus.GATED);
  });

  it("approveIrreversible lets the commit pass and the effect fires", async () => {
    const ctx = await agentTxn(f.adapters, (txn) => {
      const staged = f.tools.sendEmail({ to: "x@y.z", body: "hi" }) as StagedResult;
      txn.approveIrreversible(staged.effectId);
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(f.log.sent).toHaveLength(1);
  });

  it("a compensator-backed effect needs no approval", async () => {
    const ctx = await agentTxn(f.adapters, () => {
      f.tools.charge({ card: "tok_1", amount: 50 }); // declares compensator "refund"
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(f.log.sent.map((s) => s.tool)).toEqual(["charge"]);
  });

  it("approveIrreversible with an unknown effectId raises (typo detection)", async () => {
    await expect(
      agentTxn(f.adapters, (txn) => {
        f.tools.sendEmail({ to: "x@y.z", body: "hi" });
        txn.approveIrreversible("not-a-real-id");
      }),
    ).rejects.toThrow(/no staged effect/);
  });

  it("approval is per-effect, not blanket", async () => {
    let err: GateBlocked | undefined;
    try {
      await agentTxn(f.adapters, (txn) => {
        const a = f.tools.sendEmail({ to: "a@y.z", body: "1" }) as StagedResult;
        f.tools.sendEmail({ to: "b@y.z", body: "2" });
        txn.approveIrreversible(a.effectId); // approve only the first
      });
    } catch (e) {
      err = e as GateBlocked;
    }
    expect(err).toBeInstanceOf(GateBlocked);
    expect(err!.needsApproval).toHaveLength(1);
    expect(f.log.sent).toHaveLength(0);
  });

  it("a policy that denies between stage and commit blocks the irreversible (TOCTOU)", async () => {
    // The rule flips to Deny only on the commit-time re-walk: a counter that is
    // 0 at stage-time (rule sees nothing yet) and trips on the second pass.
    let pass = 0;
    const policy = Policy.allowAll();
    policy.rule((effect) => {
      if (effect.tool === "sendEmail") {
        pass += 1;
        if (pass > 1) return Deny("revoked before commit");
      }
      return { allow: true };
    });
    await expect(
      agentTxn(f.adapters, () => {
        f.tools.sendEmail({ to: "x@y.z", body: "hi" });
      }, { policy }),
    ).rejects.toThrow(/revoked before commit/);
    expect(f.log.sent).toHaveLength(0);
  });
});
