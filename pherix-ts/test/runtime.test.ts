/** Mirrors tests/test_runtime.py — the reversible lane: golden + rollback. */

import { beforeEach, describe, expect, it } from "vitest";
import { TxnState } from "../src/index.js";
import { agentTxn } from "../src/index.js";
import { setup, type Fixture } from "./helpers.js";

let f: Fixture;
beforeEach(() => {
  f = setup();
});

describe("reversible lane", () => {
  it("auto-commits a reversible write on clean exit (snapshot before apply, applied live)", async () => {
    const ctx = await agentTxn(f.adapters, async () => {
      await f.tools.transfer({ from: "alice", to: "bob", amount: 30 });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(f.balanceOf("alice")).toBe(70);
    expect(f.balanceOf("bob")).toBe(30);
  });

  it("explicit rollback undoes writes via the snapshot restore", async () => {
    const ctx = await agentTxn(f.adapters, async (txn) => {
      await f.tools.transfer({ from: "alice", to: "bob", amount: 30 });
      expect(f.balanceOf("bob")).toBe(30); // applied live, mid-txn
      txn.rollback();
    });
    expect(ctx.txn.state).toBe(TxnState.ROLLED_BACK);
    expect(f.balanceOf("alice")).toBe(100);
    expect(f.balanceOf("bob")).toBe(0);
  });

  it("auto-rolls-back when the agent body throws", async () => {
    let ctx: { txn: { state: TxnState } } | undefined;
    await expect(
      agentTxn(f.adapters, async (txn) => {
        ctx = txn;
        await f.tools.transfer({ from: "alice", to: "bob", amount: 30 });
        throw new Error("agent decided to abort");
      }),
    ).rejects.toThrow("agent decided to abort");
    expect(ctx!.txn.state).toBe(TxnState.ROLLED_BACK);
    expect(f.balanceOf("alice")).toBe(100);
    expect(f.balanceOf("bob")).toBe(0);
  });

  it("a tool that throws during apply rolls back cleanly (snapshot protects state)", async () => {
    await expect(
      agentTxn(f.adapters, async () => {
        await f.tools.transfer({ from: "alice", to: "bob", amount: 30 });
        await f.tools.boom({});
      }),
    ).rejects.toThrow("boom");
    // The first transfer is rolled back too — the whole txn unwinds.
    expect(f.balanceOf("alice")).toBe(100);
    expect(f.balanceOf("bob")).toBe(0);
  });
});
