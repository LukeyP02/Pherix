/** Mirrors tests/test_world_state_policy.py — #7 world-state-aware commit-time
 *  policy (TOCTOU divergence). The whole point: the policy is evaluated twice —
 *  stage-time and commit-time — so a rule reading live state can Allow at stage
 *  and Deny at commit purely because the world moved between the two walks. */

import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  Effect,
  Policy,
  PolicyContext,
  PolicyViolation,
  SqliteAdapter,
  refundIfPaid,
  sqlReader,
} from "../src/index.js";

let db: Database.Database;
let adapter: SqliteAdapter;
let adapters: Record<string, SqliteAdapter>;

beforeEach(() => {
  db = new Database(":memory:");
  db.exec("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL)");
  db.prepare("INSERT INTO orders (id, status) VALUES (?, ?)").run(42, "paid");
  adapter = new SqliteAdapter(db);
  adapters = { sql: adapter };
});

afterEach(() => {
  db.close();
});

function effect(tool: string, args: Record<string, unknown>): Effect {
  return new Effect({ txnId: "t", index: 0, tool, args, resource: "sql", reversible: true });
}

describe("the read mediator", () => {
  it("a read with no bound reader raises a clear error", () => {
    const ctx = new PolicyContext({ journal: [], where: "stage" });
    expect(() => ctx.read("sql", ["orders", "id", 42, "status"])).toThrow(/no read mediator/);
  });

  it("sqlReader returns the live value of the addressed column", () => {
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect(ctx.read("sql", ["orders", "id", 42, "status"])).toBe("paid");
  });

  it("sqlReader returns null for an absent row", () => {
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect(ctx.read("sql", ["orders", "id", 999, "status"])).toBeNull();
  });
});

describe("refundIfPaid divergence", () => {
  it("Allows at stage then Denies at commit when the order's status moves", () => {
    const rule = refundIfPaid();
    const reader = sqlReader(adapters);
    const e = effect("refundOrder", { orderId: 42 });

    // Stage-time: order is 'paid' → Allow.
    const stageCtx = new PolicyContext({ journal: [], where: "stage", reader });
    expect(rule(e, stageCtx).allow).toBe(true);

    // A concurrent actor flips the live status between the two walks.
    db.prepare("UPDATE orders SET status = ? WHERE id = ?").run("refunded", 42);

    // Commit-time: the same predicate, re-read against the moved world → Deny.
    const commitCtx = new PolicyContext({ journal: [e], where: "commit", reader });
    expect(rule(e, commitCtx).allow).toBe(false);
  });

  it("Allows at both walks when the world stays stable", () => {
    const rule = refundIfPaid();
    const reader = sqlReader(adapters);
    const e = effect("refundOrder", { orderId: 42 });
    expect(rule(e, new PolicyContext({ journal: [], where: "stage", reader })).allow).toBe(true);
    expect(rule(e, new PolicyContext({ journal: [e], where: "commit", reader })).allow).toBe(true);
  });

  it("is a no-op Allow for unrelated tools", () => {
    const rule = refundIfPaid();
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect(rule(effect("transfer", { amount: 5 }), ctx).allow).toBe(true);
  });

  it("denies (fails safe) when the id arg is missing", () => {
    const rule = refundIfPaid();
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect(rule(effect("refundOrder", {}), ctx).allow).toBe(false);
  });
});

describe("divergence through the Policy walk the runtime uses", () => {
  it("evaluate (stage) passes, evaluateJournal (commit) raises PolicyViolation at commit", () => {
    const reader = sqlReader(adapters);
    const policy = Policy.allowAll();
    policy.rule(refundIfPaid());
    const e = effect("refundOrder", { orderId: 42 });
    const ctx = new PolicyContext({ journal: [e], where: "stage", reader });

    // Stage-time walk: order paid → no raise.
    expect(() => policy.evaluate(e, ctx, "stage")).not.toThrow();

    // World moves underneath, then the commit-time re-walk denies.
    db.prepare("UPDATE orders SET status = ? WHERE id = ?").run("refunded", 42);
    try {
      policy.evaluateJournal({ effects: [e] }, ctx);
      throw new Error("expected PolicyViolation");
    } catch (err) {
      expect(err).toBeInstanceOf(PolicyViolation);
      expect((err as PolicyViolation).where).toBe("commit");
    }
  });
});

describe("layer independence (fake reader)", () => {
  it("a one-line fake reader proves the rule reads through ctx.read, not the DB", () => {
    const box = { status: "paid" };
    const reader = (): unknown => box.status;
    const rule = refundIfPaid();
    const e = effect("refundOrder", { orderId: 7 });

    expect(rule(e, new PolicyContext({ journal: [], where: "stage", reader })).allow).toBe(true);
    box.status = "cancelled";
    expect(rule(e, new PolicyContext({ journal: [], where: "commit", reader })).allow).toBe(false);
  });
});
