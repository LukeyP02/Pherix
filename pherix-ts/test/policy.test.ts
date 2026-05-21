/** Mirrors tests/test_policy.py + test_policy_runtime.py — the policy axis. */

import { beforeEach, describe, expect, it } from "vitest";
import {
  AuditJournal,
  Cap,
  Deny,
  Policy,
  PolicyViolation,
  agentTxn,
} from "../src/index.js";
import { setup, type Fixture } from "./helpers.js";

let f: Fixture;
beforeEach(() => {
  f = setup();
});

describe("Policy tool-name lists", () => {
  it("the default policy permits everything", () => {
    expect(Policy.allowAll().permits("anything")).toBe(true);
  });

  it("an allow-list restricts to listed tools", () => {
    const p = new Policy({ allow: ["transfer"] });
    expect(p.permits("transfer")).toBe(true);
    expect(p.permits("sendEmail")).toBe(false);
  });

  it("a deny-list blocks listed tools", () => {
    const p = new Policy({ deny: ["sendEmail"] });
    expect(p.permits("transfer")).toBe(true);
    expect(p.permits("sendEmail")).toBe(false);
  });

  it("deny wins over allow", () => {
    const p = new Policy({ allow: ["sendEmail"], deny: ["sendEmail"] });
    expect(p.permits("sendEmail")).toBe(false);
  });
});

describe("Policy at runtime", () => {
  it("an args-aware rule denies at stage-time and journals nothing", async () => {
    const audit = AuditJournal.inMemory();
    const policy = Policy.allowAll();
    policy.rule((effect) =>
      effect.tool === "transfer" && (effect.args.amount as number) > 50
        ? Deny("transfer over limit")
        : { allow: true },
    );

    let captured: { txn: { txnId: string; effects: unknown[] } } | undefined;
    await expect(
      agentTxn(
        f.adapters,
        (txn) => {
          captured = txn;
          f.tools.transfer({ from: "alice", to: "bob", amount: 60 });
        },
        { policy, audit },
      ),
    ).rejects.toThrow(PolicyViolation);

    // Stage-time denial means the effect never entered the journal...
    expect(captured!.txn.effects).toHaveLength(0);
    // ...and never hit the audit log either.
    expect(audit.getEffects(captured!.txn.txnId)).toHaveLength(0);
    // ...and the world is untouched.
    expect(f.balanceOf("alice")).toBe(100);
  });

  it("a count cap denies the call that would exceed it", async () => {
    const policy = Policy.withRules({ caps: [Cap.count({ tool: "sendEmail", max: 1 })] });
    await expect(
      agentTxn(
        f.adapters,
        () => {
          f.tools.sendEmail({ to: "a@y.z", body: "1" });
          f.tools.sendEmail({ to: "b@y.z", body: "2" }); // trips the cap at stage-time
        },
        { policy },
      ),
    ).rejects.toThrow(/count cap/);
    expect(f.log.sent).toHaveLength(0);
  });

  it("a sum cap denies the charge that pushes the total over max", async () => {
    const policy = Policy.withRules({
      caps: [Cap.sum({ tool: "charge", via: (a) => a.amount as number, max: 100 })],
    });
    await expect(
      agentTxn(
        f.adapters,
        () => {
          f.tools.charge({ card: "A", amount: 60 });
          f.tools.charge({ card: "B", amount: 60 }); // 120 > 100 -> Deny
        },
        { policy },
      ),
    ).rejects.toThrow(/sum cap/);
  });
});
