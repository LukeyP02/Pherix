/** Mirrors tests/test_dry_run.py — speculative execution: fold forward, then
 *  discard. The headline guarantee is that the world is bit-identical after a
 *  dry-run, while the result still reports what *would* have happened. */

import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  HttpAdapter,
  Policy,
  REGISTRY,
  type SqliteDatabase,
  SqliteAdapter,
  TxnState,
  dryRun,
  tool,
} from "../src/index.js";

let db: Database.Database;
let sql: SqliteAdapter;
let http: HttpAdapter;
let adapters: Record<string, SqliteAdapter | HttpAdapter>;
let sent: string[];

beforeEach(() => {
  REGISTRY.clear();
  db = new Database(":memory:");
  db.exec("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)");
  db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("alice", 100);
  sql = new SqliteAdapter(db);
  http = new HttpAdapter();
  adapters = { sql, http };
  sent = [];
});

afterEach(() => {
  db.close();
});

function balanceOf(name: string): number | undefined {
  const row = db.prepare("SELECT balance FROM accounts WHERE name = ?").get(name) as
    | { balance: number }
    | undefined;
  return row?.balance;
}

function tools() {
  return {
    addAccount: tool<{ name: string; balance: number }>(
      "sql",
      (conn: SqliteDatabase, args) => {
        conn.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run(args.name, args.balance);
        return { ok: true };
      },
      { name: "addAccount" },
    ),
    sendEmail: tool<{ to: string }>(
      "http",
      (args) => {
        sent.push(args.to);
        return { delivered: true };
      },
      { injectsHandle: false, name: "sendEmail" },
    ),
  };
}

describe("dryRun leaves the world untouched", () => {
  it("a reversible write does not persist and an irreversible never fires", async () => {
    const t = tools();
    const ctx = await dryRun(adapters, async () => {
      await t.addAccount({ name: "carol", balance: 50 });
      await t.sendEmail({ to: "carol@x.y" });
    });

    // The world is bit-identical: no new row, no email fired.
    expect(balanceOf("carol")).toBeUndefined();
    expect(sent).toHaveLength(0);
    expect(ctx.txn.state).toBe(TxnState.ROLLED_BACK);
  });

  it("the result reports the journal and the irreversibles that would have fired", async () => {
    const t = tools();
    const ctx = await dryRun(adapters, async () => {
      await t.addAccount({ name: "carol", balance: 50 });
      await t.sendEmail({ to: "carol@x.y" });
    });
    const r = ctx.result!;
    expect(r).not.toBeNull();
    expect(r.journal).toHaveLength(2);
    // Only the irreversible HTTP effect would have fired at commit-time.
    expect(r.wouldHaveFired).toHaveLength(1);
    expect(r.wouldHaveFired[0]!.tool).toBe("sendEmail");
    expect(r.isClean).toBe(true);
  });

  it("the state diff previews the SQL rows that would have been added", async () => {
    const t = tools();
    const ctx = await dryRun(adapters, async () => {
      await t.addAccount({ name: "carol", balance: 50 });
    });
    const diff = ctx.result!.stateDiff["sql"] as { rows_added: Array<{ row: unknown }> };
    const added = diff.rows_added.map((e) => e.row as { name: string });
    expect(added.some((row) => row.name === "carol")).toBe(true);
    // ...yet the live world is still untouched after the dry-run.
    expect(balanceOf("carol")).toBeUndefined();
  });
});

describe("dryRun captures policy verdicts without aborting", () => {
  it("a denied tool keeps the body running and lands a non-clean verdict", async () => {
    const t = tools();
    const policy = new Policy({ deny: ["sendEmail"] });
    const ctx = await dryRun(
      adapters,
      async () => {
        await t.addAccount({ name: "carol", balance: 50 });
        await t.sendEmail({ to: "carol@x.y" }); // denied — but the run continues
      },
      { policy },
    );
    const r = ctx.result!;
    // The body completed: both effects are in the journal despite the deny.
    expect(r.journal).toHaveLength(2);
    expect(r.isClean).toBe(false);
    // At least one captured verdict denies sendEmail.
    const denied = r.policyVerdicts.filter((v) => !v.allow && v.tool === "sendEmail");
    expect(denied.length).toBeGreaterThan(0);
    // The world is still untouched.
    expect(balanceOf("carol")).toBeUndefined();
    expect(sent).toHaveLength(0);
  });
});
