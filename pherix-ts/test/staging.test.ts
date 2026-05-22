/** Mirrors tests/test_runtime_staging.py — the irreversible lane never fires early. */

import { beforeEach, describe, expect, it } from "vitest";
import {
  CompensatorNotRegistered,
  EffectStatus,
  StagedResult,
  agentTxn,
  tool,
} from "../src/index.js";
import { setup, type Fixture } from "./helpers.js";

let f: Fixture;
beforeEach(() => {
  f = setup();
});

describe("irreversible staging lane", () => {
  it("returns a StagedResult sentinel and does not fire at stage-time", async () => {
    let staged: unknown;
    await agentTxn(f.adapters, async (txn) => {
      staged = await f.tools.sendEmail({ to: "x@y.z", body: "hi" });
      // The external call has NOT happened yet — staging defers it to commit.
      expect(f.log.sent).toHaveLength(0);
      txn.approveIrreversible((staged as StagedResult).effectId);
    });
    expect(staged).toBeInstanceOf(StagedResult);
    // After commit (approved) it has fired exactly once.
    expect(f.log.sent).toHaveLength(1);
  });

  it("the StagedResult effectId matches the journal entry", async () => {
    let staged: StagedResult | undefined;
    const ctx = await agentTxn(f.adapters, async (txn) => {
      staged = (await f.tools.sendEmail({ to: "x@y.z", body: "hi" })) as StagedResult;
      await txn.rollback(); // we only need the journal entry, not a commit
    });
    const entry = ctx.txn.effects.find((e) => e.tool === "sendEmail");
    expect(staged!.effectId).toBe(entry!.effectId);
  });

  it("a staged effect has no snapshot, records reversible=false from the adapter", async () => {
    let snapshotMidTxn: unknown;
    let reversibleMidTxn: boolean | undefined;
    await agentTxn(f.adapters, async (txn) => {
      await f.tools.sendEmail({ to: "x@y.z", body: "hi" });
      const e = txn.txn.effects[0]!;
      snapshotMidTxn = e.snapshot;
      reversibleMidTxn = e.reversible;
      expect(e.status).toBe(EffectStatus.STAGED);
      await txn.rollback(); // avoid the gate; we are asserting mid-txn shape only
    });
    expect(snapshotMidTxn).toBeNull();
    expect(reversibleMidTxn).toBe(false);
  });

  it("rollback before commit means staged effects never fire (strongest containment)", async () => {
    const ctx = await agentTxn(f.adapters, async (txn) => {
      await f.tools.sendEmail({ to: "x@y.z", body: "hi" });
      await txn.rollback();
    });
    expect(ctx.txn.state).toBe("rolled_back");
    expect(f.log.sent).toHaveLength(0);
  });

  it("an exception in the agent body does not fire staged effects", async () => {
    await expect(
      agentTxn(f.adapters, async () => {
        await f.tools.sendEmail({ to: "x@y.z", body: "hi" });
        throw new Error("abort");
      }),
    ).rejects.toThrow("abort");
    expect(f.log.sent).toHaveLength(0);
  });

  it("a compensator typo raises at stage-time before any state change", async () => {
    // setup() already cleared + repopulated the registry in beforeEach; register
    // one more tool whose declared compensator does not exist.
    const bad = tool("http", (_args: Record<string, unknown>) => {}, {
      injectsHandle: false,
      name: "bad",
      compensator: "does_not_exist",
    });
    await expect(
      agentTxn(f.adapters, async () => {
        await bad({});
      }),
    ).rejects.toThrow(CompensatorNotRegistered);
  });
});
