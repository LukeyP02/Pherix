/** Mirrors tests/test_adapters_postgres.py — the Postgres savepoint lane.
 *
 * Python's PG test runs against a live database and skips when none is
 * reachable. We additionally prove the savepoint lane *offline*: a FakePgClient
 * backed by better-sqlite3 implements the async `query(text, params)` surface
 * (SQLite speaks the same SAVEPOINT / ROLLBACK TO SAVEPOINT grammar), so the
 * real PostgresAdapter code path is exercised end-to-end with no live PG. */

import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  type Effect,
  type PgClient,
  type PgResult,
  PostgresAdapter,
  REGISTRY,
  TxnState,
  agentTxn,
  tool,
} from "../src/index.js";

/** An async `pg`-shaped client over an in-memory SQLite db. `$n` placeholders
 *  are rewritten to positional `?`; SELECT / RETURNING use `.all`, everything
 *  else `.run` (params) or `.exec` (transaction-control statements). */
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

let db: Database.Database;
let client: FakePgClient;
let adapter: PostgresAdapter;

beforeEach(() => {
  REGISTRY.clear();
  db = new Database(":memory:");
  db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)");
  client = new FakePgClient(db);
  adapter = new PostgresAdapter(client);
});

afterEach(() => {
  db.close();
});

function effect(index: number, toolName: string, args: Record<string, unknown>): Effect {
  // Construct a bare Effect for the direct adapter-level tests.
  return {
    txnId: "t",
    index,
    tool: toolName,
    args,
    resource: "postgres",
    reversible: true,
    effectId: `e${index}`,
    readKeys: [],
    writeKeys: [],
    status: "staged",
    snapshot: null,
    result: null,
    compensator: null,
    ts: new Date(),
  } as unknown as Effect;
}

function count(): number {
  return (db.prepare("SELECT COUNT(*) AS c FROM users").get() as { c: number }).c;
}

describe("PostgresAdapter contract", () => {
  it("names itself postgres and is honest about rollback", () => {
    expect(adapter.name).toBe("postgres");
    expect(adapter.supportsRollback()).toBe(true);
  });

  it("derives the savepoint name from the effect index", async () => {
    await adapter.begin();
    try {
      const h = await adapter.snapshot(effect(5, "x", { name: "bob" }));
      expect(h.payload["savepoint"]).toBe("sp_5");
    } finally {
      await adapter.rollback();
    }
  });
});

describe("PostgresAdapter savepoint round-trip", () => {
  it("snapshot -> apply -> ROLLBACK TO SAVEPOINT undoes the insert", async () => {
    const insertUser = (c: PgClient, args: { name: string }) =>
      c.query("INSERT INTO users (name) VALUES ($1)", [args.name]);

    await adapter.begin();
    try {
      const e = effect(0, "insert_user", { name: "bob" });
      const handle = await adapter.snapshot(e);
      await adapter.apply(e, insertUser as never);
      expect(count()).toBe(1);
      await adapter.restore(handle);
      expect(count()).toBe(0); // savepoint restore reverted the insert
    } finally {
      await adapter.rollback();
    }
  });
});

describe("PostgresAdapter under the runtime", () => {
  function insertTool() {
    return tool<{ name: string }>(
      "postgres",
      (c: PgClient, args) => c.query("INSERT INTO users (name) VALUES ($1)", [args.name]),
      { name: "insertUser" },
    );
  }

  it("auto-commits inserts on clean exit", async () => {
    const insertUser = insertTool();
    const ctx = await agentTxn({ postgres: adapter }, async () => {
      await insertUser({ name: "alice" });
      await insertUser({ name: "bob" });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(count()).toBe(2);
  });

  it("rolls back every insert when the agent body throws", async () => {
    const insertUser = insertTool();
    await expect(
      agentTxn({ postgres: adapter }, async () => {
        await insertUser({ name: "alice" });
        throw new Error("abort");
      }),
    ).rejects.toThrow("abort");
    expect(count()).toBe(0);
  });
});
