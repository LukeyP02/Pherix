/** Mirrors tests/test_isolation_self_write.py — the committed-only read path.
 *
 * On an ON-DISK SQLite database the adapter opens a separate autocommit "meta"
 * connection (SqliteAdapter.readsCommittedOnly() → true) whose reads of the
 * version side-table see only COMMITTED state — never this txn's own
 * uncommitted bumps. The commit-time diff (checkConflicts) reconciles the two
 * readVersion visibilities:
 *
 *   - committed-only (on-disk, meta connection): my own uncommitted writes are
 *     invisible at read AND at commit, so they cancel — compare committed-base-
 *     at-read (vAtRead) against committed-base-now (vNow). This (a) stops a
 *     read-then-write of the SAME key from self-conflicting, and (b) still
 *     catches a cross-connection committed write to a key I only read.
 *   - own-write-visible (:memory: main connection): readVersion reflects my
 *     bumps, so the diff keeps using lastMyWrite — the default branch, unchanged.
 *
 * This brings the TS SDK to parity with Python's _meta_conn path: previously
 * the TS readsCommittedOnly() was hardcoded false and the committed-only path
 * deferred, so a cross-connection lost update on a read-only key slipped past
 * the on-disk diff (the main connection's stale read snapshot hid it).
 *
 * On-disk fixtures use WAL (matching Python's on_disk_db) so a reader and a
 * separate committed writer can coexist without "database is locked". */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  Effect,
  REGISTRY,
  type SqliteDatabase,
  SqliteAdapter,
  TxnState,
  agentTxn,
  checkConflicts,
  executeIsolated,
  tool,
} from "../src/index.js";

function seed(db: SqliteDatabase): void {
  db.exec("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, val INTEGER)");
  db.prepare("INSERT OR IGNORE INTO counters (name, val) VALUES ('x', 0)").run();
}

/** read x (records a read of ["counters", name]) / write x (records a write). */
function rwTools() {
  const readX = tool<{ name: string }>(
    "sql",
    (conn: SqliteDatabase, args) => {
      const rows = executeIsolated(conn, "SELECT val FROM counters WHERE name = ?", [args.name], {
        reads: [["counters", args.name]],
      }) as Array<{ val: number }>;
      return rows[0]?.val ?? null;
    },
    { name: "readX" },
  );
  const writeX = tool<{ name: string; val: number }>(
    "sql",
    (conn: SqliteDatabase, args) => {
      executeIsolated(conn, "UPDATE counters SET val = ? WHERE name = ?", [args.val, args.name], {
        writes: [["counters", args.name]],
      });
      return { ok: true };
    },
    { name: "writeX" },
  );
  return { readX, writeX };
}

function effectWith(readKeys: Effect["readKeys"], writeKeys: Effect["writeKeys"]): Effect {
  const e = new Effect({ txnId: "t", index: 0, tool: "x", args: {}, resource: "sql", reversible: true });
  e.readKeys = readKeys;
  e.writeKeys = writeKeys;
  return e;
}

describe("SqliteAdapter — committed-only meta connection (on-disk)", () => {
  let dir: string;
  let file: string;

  beforeEach(() => {
    REGISTRY.clear();
    dir = mkdtempSync(path.join(tmpdir(), "pherix_committed_"));
    file = path.join(dir, "ledger.db");
    const boot = new Database(file);
    boot.pragma("journal_mode = WAL");
    seed(boot);
    boot.close();
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("readsCommittedOnly is true on-disk, false in-memory", () => {
    const disk = new Database(file);
    expect(new SqliteAdapter(disk).readsCommittedOnly()).toBe(true);
    disk.close();

    const mem = new Database(":memory:");
    expect(new SqliteAdapter(mem).readsCommittedOnly()).toBe(false);
    mem.close();
  });

  it("readVersion sees only committed bumps — an in-BEGIN write is invisible until commit", () => {
    const db = new Database(file);
    const a = new SqliteAdapter(db);
    const K = ["counters", "x"];

    // Autocommit bump → committed version 1, visible via the meta connection.
    expect(a.writeVersion(K)).toBe(1);
    expect(a.readVersion(K)).toBe(1);

    // Bump again inside an open txn: uncommitted on the main connection.
    a.begin();
    expect(a.writeVersion(K)).toBe(2);
    // The committed-only meta read still sees the committed base (1), NOT my own
    // uncommitted bump. (Prior commit: one connection saw its own bump → 2.)
    expect(a.readVersion(K)).toBe(1);
    a.commit();
    // Now committed → the meta read advances to 2.
    expect(a.readVersion(K)).toBe(2);

    db.close();
  });

  it("Matrix #1 — read then write the same key, no other writer, commits cleanly", async () => {
    const db = new Database(file);
    const a = new SqliteAdapter(db);
    const { readX, writeX } = rwTools();

    const ctx = await agentTxn({ sql: a }, async () => {
      expect(await readX({ name: "x" })).toBe(0);
      await writeX({ name: "x", val: 5 });
    });

    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect((db.prepare("SELECT val FROM counters WHERE name='x'").get() as { val: number }).val).toBe(5);
    db.close();
  });

  it("Matrix #4 — a cross-connection committed write to a read-only key is caught by the committed-only diff", () => {
    const dbA = new Database(file);
    const a = new SqliteAdapter(dbA);
    const dbB = new Database(file);
    const b = new SqliteAdapter(dbB);
    const K = ["counters", "x"];

    // A reads x at committed base 0 (through its meta connection) inside its txn.
    // It never writes x, so it holds no lock on the version row.
    a.begin();
    const vAtRead = a.readVersion(K);
    expect(vAtRead).toBe(0);
    const eff = effectWith([["sql", K, vAtRead]], []);

    // Another connection commits a write to x — free to proceed under WAL.
    expect(b.writeVersion(K)).toBe(1);

    // A's commit-time diff sees the moved committed base via its meta connection
    // and fires: a lost update on a key A only read. (Prior commit: A's single
    // connection kept a stale read snapshot and the diff swallowed it.)
    const conflicts = checkConflicts([eff], { sql: a });
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0]!.key).toEqual(K);
    expect(conflicts[0]!.versionAtRead).toBe(0);
    expect(conflicts[0]!.versionExpected).toBe(0); // committed base at read, NOT lastMyWrite
    expect(conflicts[0]!.versionNow).toBe(1); // B's committed bump
    a.rollback();

    dbA.close();
    dbB.close();
  });
});
