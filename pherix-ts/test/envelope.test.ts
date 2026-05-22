/** Mirrors tests/test_envelope.py — the longitudinal envelope (#10): durable
 *  cross-run spend caps. The headline guarantees: budget persists across a
 *  simulated process restart, and only a committed txn consumes it. */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  AuditJournal,
  DurableCap,
  EnvelopeStore,
  HttpAdapter,
  Policy,
  PolicyViolation,
  REGISTRY,
  TxnState,
  agentTxn,
  dayPeriod,
  tool,
} from "../src/index.js";

let dir: string;
let dbPath: string;
let sent: string[];

beforeEach(() => {
  REGISTRY.clear();
  dir = mkdtempSync(path.join(tmpdir(), "pherix_env_"));
  dbPath = path.join(dir, "audit.db");
  sent = [];
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function chargeTool() {
  // A compensator-backed irreversible: it clears the commit gate (no approval
  // needed) and auto-commits, so committed fires actually consume budget.
  tool<{ amount: number }>("http", () => ({ refunded: true }), {
    injectsHandle: false,
    name: "refund",
  });
  return tool<{ amount: number }>(
    "http",
    (args) => {
      sent.push(`charge:${args.amount}`);
      return { charged: true };
    },
    { injectsHandle: false, name: "charge", compensator: "refund" },
  );
}

describe("EnvelopeStore", () => {
  it("absent total reads 0, add returns the new running total", () => {
    const store = EnvelopeStore.fromPath(dbPath);
    expect(store.total("cap", dayPeriod())).toBe(0);
    expect(store.add("cap", dayPeriod(), 30)).toBe(30);
    expect(store.add("cap", dayPeriod(), 70)).toBe(100);
    expect(store.total("cap", dayPeriod())).toBe(100);
  });

  it("a fresh handle on the same file observes prior totals (restart survival)", () => {
    const period = dayPeriod();
    const first = EnvelopeStore.fromPath(dbPath);
    first.add("daily", period, 90);

    // Simulate a process restart: a brand-new handle on the same file.
    const afterRestart = EnvelopeStore.fromPath(dbPath);
    expect(afterRestart.total("daily", period)).toBe(90);
  });
});

describe("durable sum cap across runs", () => {
  it("denies once the persisted total + this charge would exceed max", async () => {
    const charge = chargeTool();
    const period = () => "fixed-period"; // pin the bucket so runs share it

    // Run 1: spend 60 (under the 100 cap) and commit — budget persists.
    {
      const audit = new AuditJournal(dbPath);
      const store = EnvelopeStore.fromAudit(audit);
      const policy = Policy.withRules({
        caps: [DurableCap.sum({ tool: "charge", via: (a) => a.amount as number, max: 100, store, period })],
      });
      const ctx = await agentTxn({ http: new HttpAdapter() }, async () => {
        await charge({ amount: 60 });
      }, { policy, audit });
      expect(ctx.txn.state).toBe(TxnState.COMMITTED);
      expect(store.total(
        `DurableCap.sum(tool="charge", max=100)`, "fixed-period",
      )).toBe(60);
      audit.close();
    }

    // Run 2 (fresh process): a 60 charge would push 60+60=120 > 100 → denied.
    {
      const audit = new AuditJournal(dbPath);
      const store = EnvelopeStore.fromAudit(audit);
      const policy = Policy.withRules({
        caps: [DurableCap.sum({ tool: "charge", via: (a) => a.amount as number, max: 100, store, period })],
      });
      await expect(
        agentTxn({ http: new HttpAdapter() }, async () => {
          await charge({ amount: 60 });
        }, { policy, audit }),
      ).rejects.toThrow(PolicyViolation);
      // Denied at stage-time → the charge never fired, budget unchanged at 60.
      expect(store.total(`DurableCap.sum(tool="charge", max=100)`, "fixed-period")).toBe(60);
      audit.close();
    }
  });

  it("a rolled-back txn consumes no budget", async () => {
    const charge = chargeTool();
    const period = () => "fixed-period";
    const audit = new AuditJournal(dbPath);
    const store = EnvelopeStore.fromAudit(audit);
    const policy = Policy.withRules({
      caps: [DurableCap.sum({ tool: "charge", via: (a) => a.amount as number, max: 100, store, period })],
    });

    await expect(
      agentTxn({ http: new HttpAdapter() }, async () => {
        await charge({ amount: 40 });
        throw new Error("abort before commit");
      }, { policy, audit }),
    ).rejects.toThrow("abort before commit");

    // Rolled back → the charge never fired and budget stays at 0.
    expect(store.total(`DurableCap.sum(tool="charge", max=100)`, "fixed-period")).toBe(0);
    expect(sent).toHaveLength(0);
    audit.close();
  });
});

describe("durable count cap", () => {
  it("counts committed fires across runs and denies past max", async () => {
    const charge = chargeTool();
    const period = () => "fixed-period";
    const capName = `DurableCap.count(tool="charge", max=2)`;

    const run = async (): Promise<TxnState> => {
      const audit = new AuditJournal(dbPath);
      const store = EnvelopeStore.fromAudit(audit);
      const policy = Policy.withRules({
        caps: [DurableCap.count({ tool: "charge", max: 2, store, period })],
      });
      let state: TxnState;
      try {
        const ctx = await agentTxn({ http: new HttpAdapter() }, async () => {
          await charge({ amount: 1 });
        }, { policy, audit });
        state = ctx.txn.state;
      } catch (e) {
        if (e instanceof PolicyViolation) state = TxnState.ROLLED_BACK;
        else throw e;
      }
      audit.close();
      return state;
    };

    expect(await run()).toBe(TxnState.COMMITTED); // 1st fire
    expect(await run()).toBe(TxnState.COMMITTED); // 2nd fire — at the cap
    // 3rd fire would be the 3rd count > max=2 → denied.
    expect(await run()).toBe(TxnState.ROLLED_BACK);

    const store = EnvelopeStore.fromPath(dbPath);
    expect(store.total(capName, "fixed-period")).toBe(2);
  });
});
