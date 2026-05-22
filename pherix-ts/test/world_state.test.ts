/** Mirrors tests/test_world_state_policy.py — #7 world-state-aware commit-time
 *  policy (TOCTOU divergence). The whole point: the policy is evaluated twice —
 *  stage-time and commit-time — so a rule reading live state can Allow at stage
 *  and Deny at commit purely because the world moved between the two walks.
 *
 *  Reads are async (a rule `await`s ctx.read), so the rule works uniformly over
 *  a synchronous SQLite connection and an asynchronous Postgres one. Both are
 *  exercised below. */

import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  Effect,
  type PgClient,
  type PgResult,
  Policy,
  PolicyContext,
  PolicyViolation,
  PostgresAdapter,
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
  it("a read with no bound reader raises a clear error", async () => {
    const ctx = new PolicyContext({ journal: [], where: "stage" });
    await expect(ctx.read("sql", ["orders", "id", 42, "status"])).rejects.toThrow(
      /no read mediator/,
    );
  });

  it("sqlReader returns the live value of the addressed column", async () => {
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect(await ctx.read("sql", ["orders", "id", 42, "status"])).toBe("paid");
  });

  it("sqlReader returns null for an absent row", async () => {
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect(await ctx.read("sql", ["orders", "id", 999, "status"])).toBeNull();
  });
});

describe("refundIfPaid divergence (SQLite, sync connection)", () => {
  it("Allows at stage then Denies at commit when the order's status moves", async () => {
    const rule = refundIfPaid();
    const reader = sqlReader(adapters);
    const e = effect("refundOrder", { orderId: 42 });

    // Stage-time: order is 'paid' → Allow.
    const stageCtx = new PolicyContext({ journal: [], where: "stage", reader });
    expect((await rule(e, stageCtx)).allow).toBe(true);

    // A concurrent actor flips the live status between the two walks.
    db.prepare("UPDATE orders SET status = ? WHERE id = ?").run("refunded", 42);

    // Commit-time: the same predicate, re-read against the moved world → Deny.
    const commitCtx = new PolicyContext({ journal: [e], where: "commit", reader });
    expect((await rule(e, commitCtx)).allow).toBe(false);
  });

  it("Allows at both walks when the world stays stable", async () => {
    const rule = refundIfPaid();
    const reader = sqlReader(adapters);
    const e = effect("refundOrder", { orderId: 42 });
    expect((await rule(e, new PolicyContext({ journal: [], where: "stage", reader }))).allow).toBe(
      true,
    );
    expect((await rule(e, new PolicyContext({ journal: [e], where: "commit", reader }))).allow).toBe(
      true,
    );
  });

  it("is a no-op Allow for unrelated tools", async () => {
    const rule = refundIfPaid();
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect((await rule(effect("transfer", { amount: 5 }), ctx)).allow).toBe(true);
  });

  it("denies (fails safe) when the id arg is missing", async () => {
    const rule = refundIfPaid();
    const ctx = new PolicyContext({ journal: [], where: "stage", reader: sqlReader(adapters) });
    expect((await rule(effect("refundOrder", {}), ctx)).allow).toBe(false);
  });
});

describe("divergence through the Policy walk the runtime uses", () => {
  it("evaluate (stage) passes, evaluateJournal (commit) raises PolicyViolation at commit", async () => {
    const reader = sqlReader(adapters);
    const policy = Policy.allowAll();
    policy.rule(refundIfPaid());
    const e = effect("refundOrder", { orderId: 42 });
    const ctx = new PolicyContext({ journal: [e], where: "stage", reader });

    // Stage-time walk: order paid → no raise.
    await expect(policy.evaluate(e, ctx, "stage")).resolves.toBeUndefined();

    // World moves underneath, then the commit-time re-walk denies.
    db.prepare("UPDATE orders SET status = ? WHERE id = ?").run("refunded", 42);
    await expect(policy.evaluateJournal({ effects: [e] }, ctx)).rejects.toBeInstanceOf(
      PolicyViolation,
    );
  });
});

describe("layer independence (fake reader)", () => {
  it("a one-line fake reader proves the rule reads through ctx.read, not the DB", async () => {
    const box = { status: "paid" };
    const reader = (): unknown => box.status;
    const rule = refundIfPaid();
    const e = effect("refundOrder", { orderId: 7 });

    expect((await rule(e, new PolicyContext({ journal: [], where: "stage", reader }))).allow).toBe(
      true,
    );
    box.status = "cancelled";
    expect((await rule(e, new PolicyContext({ journal: [], where: "commit", reader }))).allow).toBe(
      false,
    );
  });
});

// --- the async path: world-state policy over a Postgres-shaped driver -------

/** The same sqlite-backed async `pg` client used in postgres.test.ts. */
class FakePgClient implements PgClient {
  constructor(public readonly db: Database.Database) {}
  async query(text: string, params: unknown[] = []): Promise<PgResult> {
    const sql = text.replace(/\$\d+/g, "?");
    if (/^\s*select/i.test(sql) || /\breturning\b/i.test(sql)) {
      const rows = this.db.prepare(sql).all(...(params as never[])) as Array<
        Record<string, unknown>
      >;
      return { rows };
    }
    if (params.length > 0) {
      this.db.prepare(sql).run(...(params as never[]));
      return { rows: [] };
    }
    this.db.exec(sql);
    return { rows: [] };
  }
}

describe("refundIfPaid divergence (Postgres, async connection)", () => {
  it("the async read path diverges identically: Allow at stage, Deny at commit", async () => {
    const pgDb = new Database(":memory:");
    pgDb.exec("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL)");
    pgDb.prepare("INSERT INTO orders (id, status) VALUES (?, ?)").run(7, "paid");
    try {
      const pg = new PostgresAdapter(new FakePgClient(pgDb));
      const reader = sqlReader({ postgres: pg });
      const rule = refundIfPaid({ resource: "postgres" });
      const e = new Effect({
        txnId: "t",
        index: 0,
        tool: "refundOrder",
        args: { orderId: 7 },
        resource: "postgres",
        reversible: true,
      });

      // Stage-time over the async driver → 'paid' → Allow.
      expect((await rule(e, new PolicyContext({ journal: [], where: "stage", reader }))).allow).toBe(
        true,
      );

      pgDb.prepare("UPDATE orders SET status = ? WHERE id = ?").run("refunded", 7);

      // Commit-time → 'refunded' → Deny. The await on ctx.read is what lets the
      // synchronous rule contract host an asynchronous database read.
      expect(
        (await rule(e, new PolicyContext({ journal: [e], where: "commit", reader }))).allow,
      ).toBe(false);
    } finally {
      pgDb.close();
    }
  });
});
