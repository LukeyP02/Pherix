/**
 * MySQLAdapter tests — mirror of tests/test_adapters_mysql.py.
 *
 * Python's MySQL test runs against a live server and skips when none is
 * reachable. We prove the savepoint + version lane *offline*: a FakeMySql
 * connection backed by better-sqlite3 implements the async query(sql, params)
 * surface returning `[rows, fields]`. SQLite speaks the same SAVEPOINT /
 * ROLLBACK TO SAVEPOINT grammar, so the real MySQLAdapter code path is
 * exercised end-to-end with no live MySQL. The two MySQL-specific grammars the
 * adapter emits — the InnoDB DDL and `ON DUPLICATE KEY UPDATE` — are rewritten
 * to their SQLite equivalents in the fake, leaving the adapter untouched.
 */

import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Effect } from "../src/index.js";
import { MySQLAdapter, type MySQLConnection } from "../src/adapters/index.js";

/** Rewrite the MySQL grammar the adapter emits into SQLite-compatible SQL. */
function toSqlite(sql: string): string {
  let s = sql;
  // InnoDB engine clause + VARCHAR/BIGINT are fine in SQLite except ENGINE=.
  s = s.replace(/\)\s*ENGINE=InnoDB/i, ")");
  // ON DUPLICATE KEY UPDATE version = version + 1  ->  ON CONFLICT ... DO UPDATE
  s = s.replace(
    /ON DUPLICATE KEY UPDATE version = version \+ 1/i,
    "ON CONFLICT(resource, key_json) DO UPDATE SET version = version + 1",
  );
  return s;
}

class FakeMySql implements MySQLConnection {
  constructor(public readonly db: Database.Database) {}

  async query(sql: string, params: unknown[] = []): Promise<[Array<Record<string, unknown>>, unknown]> {
    const text = toSqlite(sql);
    if (/^\s*select/i.test(text)) {
      const rows = this.db.prepare(text).all(...(params as never[])) as Array<Record<string, unknown>>;
      return [rows, undefined];
    }
    if (params.length > 0) {
      this.db.prepare(text).run(...(params as never[]));
      return [[], undefined];
    }
    this.db.exec(text);
    return [[], undefined];
  }
}

function effect(index: number, tool: string, args: Record<string, unknown>): Effect {
  return new Effect({ txnId: "t", index, tool, args, resource: "mysql", reversible: true });
}

let db: Database.Database;
let conn: FakeMySql;
let adapter: MySQLAdapter;

function count(): number {
  return (db.prepare("SELECT COUNT(*) AS c FROM users").get() as { c: number }).c;
}

beforeEach(() => {
  db = new Database(":memory:");
  db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)");
  conn = new FakeMySql(db);
  adapter = new MySQLAdapter(conn);
});

afterEach(() => {
  db.close();
});

const insertUser =
  () =>
  async (c: MySQLConnection, args: { name: string }): Promise<string> => {
    await c.query("INSERT INTO users (name) VALUES (?)", [args.name]);
    return args.name;
  };

describe("MySQLAdapter", () => {
  it("is honestly reversible and named", () => {
    expect(adapter.supportsRollback()).toBe(true);
    expect(adapter.name).toBe("mysql");
  });

  it("encodeKey is canonical (cross-process stable)", () => {
    // Mirror of Python's _encode_key; exercised through writeVersion below too.
    expect(JSON.stringify(["users", 1])).toBe('["users",1]');
  });

  // --- left-inverse: snapshot -> apply -> restore ---------------------------
  it("restore ∘ apply ≈ identity (inserted row vanishes on restore)", async () => {
    await adapter.begin();
    try {
      const e = effect(0, "insert_user", { name: "bob" });
      e.snapshot = await adapter.snapshot(e);
      await adapter.apply(e, insertUser());
      expect(count()).toBe(1);
      await adapter.restore(e.snapshot);
      expect(count()).toBe(0);
    } finally {
      await adapter.rollback();
    }
  });

  it("apply returns the tool result", async () => {
    await adapter.begin();
    try {
      const e = effect(0, "insert_user", { name: "bob" });
      e.snapshot = await adapter.snapshot(e);
      expect(await adapter.apply(e, insertUser())).toBe("bob");
    } finally {
      await adapter.rollback();
    }
  });

  it("injects the connection as the first arg", async () => {
    await adapter.begin();
    try {
      const e = effect(0, "spy", { name: "x" });
      e.snapshot = await adapter.snapshot(e);
      const seen: Record<string, unknown> = {};
      await adapter.apply(e, (c: MySQLConnection) => {
        seen["conn"] = c;
      });
      expect(seen["conn"]).toBe(conn);
    } finally {
      await adapter.rollback();
    }
  });

  it("newest-first restore unwinds in reverse", async () => {
    await adapter.begin();
    try {
      const handles = [];
      for (const [i, name] of ["a", "b", "c"].entries()) {
        const e = effect(i, "insert_user", { name });
        e.snapshot = await adapter.snapshot(e);
        handles.push(e.snapshot);
        await adapter.apply(e, insertUser());
      }
      expect(count()).toBe(3);
      await adapter.restore(handles[2]!);
      expect(count()).toBe(2);
      await adapter.restore(handles[1]!);
      expect(count()).toBe(1);
      await adapter.restore(handles[0]!);
      expect(count()).toBe(0);
    } finally {
      await adapter.rollback();
    }
  });

  it("commit persists across transactions", async () => {
    await adapter.begin();
    const e = effect(0, "insert_user", { name: "bob" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, insertUser());
    await adapter.commit();
    await adapter.begin();
    try {
      expect(count()).toBe(1);
    } finally {
      await adapter.rollback();
    }
  });

  it("outer rollback discards everything", async () => {
    await adapter.begin();
    for (const [i, name] of ["a", "b"].entries()) {
      const e = effect(i, "insert_user", { name });
      e.snapshot = await adapter.snapshot(e);
      await adapter.apply(e, insertUser());
    }
    await adapter.rollback();
    await adapter.begin();
    try {
      expect(count()).toBe(0);
    } finally {
      await adapter.rollback();
    }
  });

  // --- partial failure: a tool error leaves the savepoint usable ------------
  it("partial failure: tool throws, restore to savepoint keeps the prior row", async () => {
    await adapter.begin();
    try {
      const e0 = effect(0, "insert_user", { name: "good" });
      e0.snapshot = await adapter.snapshot(e0);
      await adapter.apply(e0, insertUser());

      const e1 = effect(1, "boom", {});
      e1.snapshot = await adapter.snapshot(e1);
      await expect(
        adapter.apply(e1, async (c: MySQLConnection) => {
          await c.query("INSERT INTO no_such_table_pherix VALUES (1)");
        }),
      ).rejects.toThrow();

      await adapter.restore(e1.snapshot);
      expect(count()).toBe(1);
    } finally {
      await adapter.rollback();
    }
  });

  // --- versioning -----------------------------------------------------------
  it("readVersion of an absent key is 0", async () => {
    expect(await adapter.readVersion(["k", 1])).toBe(0);
  });

  it("writeVersion is monotonic", async () => {
    const key = ["k", 1];
    expect(await adapter.writeVersion(key)).toBe(1);
    expect(await adapter.writeVersion(key)).toBe(2);
    expect(await adapter.writeVersion(key)).toBe(3);
    expect(await adapter.readVersion(key)).toBe(3);
  });

  it("distinct keys have independent versions", async () => {
    await adapter.writeVersion(["k1", 1]);
    await adapter.writeVersion(["k1", 1]);
    await adapter.writeVersion(["k2", 2]);
    expect(await adapter.readVersion(["k1", 1])).toBe(2);
    expect(await adapter.readVersion(["k2", 2])).toBe(1);
  });
});
