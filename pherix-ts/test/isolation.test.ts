/** Mirrors tests/test_isolation.py — the commit-time conflict diff (#8).
 *
 * A conflict is a non-commutativity event: a key this txn READ was written by
 * someone else between the read and the commit (a lost update). The diff
 * (checkConflicts) detects it; the resolution policy (Abort default) decides
 * what to do. Self-bumps — a key I read then wrote myself — must NOT count.
 *
 * Single-connection in-process tier, plus the on-disk meta-connection path
 * (readsCommittedOnly: true). The cross-process intent ledger and the
 * Retry-via-run-loop are deliberately deferred (see isolation.ts). */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  Abort,
  Effect,
  IsolationConflict,
  ISOLATION_REGISTRY,
  JournalRegistry,
  REGISTRY,
  type ResourceAdapter,
  Serialize,
  type SqliteDatabase,
  SqliteAdapter,
  TxnState,
  agentTxn,
  checkConflicts,
  executeIsolated,
  tool,
} from "../src/index.js";

// --- unit: the diff over a controllable versioned adapter -------------------

/** A versioned adapter whose key versions are set by hand, so the diff's
 *  disambiguation can be pinned precisely. */
class FakeVersioned implements ResourceAdapter {
  readonly name = "fake";
  readonly versions = new Map<string, number>();
  supportsRollback(): boolean {
    return true;
  }
  readVersion(key: unknown): number {
    return this.versions.get(JSON.stringify(key)) ?? 0;
  }
  // unused by the diff, present to satisfy the protocol
  snapshot(): never {
    throw new Error("unused");
  }
  apply(): never {
    throw new Error("unused");
  }
  restore(): void {}
}

function effectWith(readKeys: Effect["readKeys"], writeKeys: Effect["writeKeys"]): Effect {
  const e = new Effect({ txnId: "t", index: 0, tool: "x", args: {}, resource: "fake", reversible: true });
  e.readKeys = readKeys;
  e.writeKeys = writeKeys;
  return e;
}

describe("checkConflicts", () => {
  it("fires when a read key was written since (lost update)", () => {
    const a = new FakeVersioned();
    a.versions.set(JSON.stringify(["x"]), 1); // someone bumped x after our read
    const e = effectWith([["fake", ["x"], 0]], []);
    const conflicts = checkConflicts([e], { fake: a });
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0]!.key).toEqual(["x"]);
    expect(conflicts[0]!.versionNow).toBe(1);
    expect(conflicts[0]!.versionExpected).toBe(0);
  });

  it("does not fire when the read key is unchanged", () => {
    const a = new FakeVersioned(); // x still at 0
    const e = effectWith([["fake", ["x"], 0]], []);
    expect(checkConflicts([e], { fake: a })).toHaveLength(0);
  });

  it("a self-bump (read then write the same key) is not a conflict", () => {
    const a = new FakeVersioned();
    a.versions.set(JSON.stringify(["x"]), 1); // my own write bumped it to 1
    const e = effectWith([["fake", ["x"], 0]], [["fake", ["x"], 1]]);
    expect(checkConflicts([e], { fake: a })).toHaveLength(0);
  });

  it("a cross-txn write ON TOP of my self-bump still fires (lost update survives)", () => {
    const a = new FakeVersioned();
    a.versions.set(JSON.stringify(["x"]), 2); // mine bumped to 1, another bumped to 2
    const e = effectWith([["fake", ["x"], 0]], [["fake", ["x"], 1]]);
    const conflicts = checkConflicts([e], { fake: a });
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0]!.versionExpected).toBe(1); // expected my-last-write
    expect(conflicts[0]!.versionNow).toBe(2); // someone moved it past that
  });

  it("skips non-versioned / irreversible adapters", () => {
    const e = effectWith([["http", ["x"], 0]], []);
    // no adapter registered for "http" in this map → skipped, no throw
    expect(checkConflicts([e], {})).toHaveLength(0);
  });
});

// --- end-to-end: through the runtime + real SQLite + executeIsolated --------

describe("isolation through the runtime (Abort default)", () => {
  let db: Database.Database;
  let sql: SqliteAdapter;

  beforeEach(() => {
    REGISTRY.clear();
    db = new Database(":memory:");
    db.exec("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)");
    db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("alice", 100);
    sql = new SqliteAdapter(db);
  });

  afterEach(() => db.close());

  function readTool() {
    return tool<{ name: string }>(
      "sql",
      (conn: SqliteDatabase, args) =>
        executeIsolated(conn, "SELECT balance FROM accounts WHERE name = ?", [args.name], {
          reads: [["accounts", args.name]],
        }),
      { name: "readBalance" },
    );
  }

  it("a pure read with no concurrent writer commits cleanly", async () => {
    const readBalance = readTool();
    const ctx = await agentTxn({ sql }, async () => {
      await readBalance({ name: "alice" });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
  });

  it("a read whose key is written before commit aborts the txn", async () => {
    const readBalance = readTool();
    await expect(
      agentTxn({ sql }, async () => {
        await readBalance({ name: "alice" }); // records read of alice at version 0
        // Simulate a concurrent committed write to alice — the side-effect
        // another txn's executeIsolated write would have on the version.
        sql.writeVersion(["accounts", "alice"]);
      }),
    ).rejects.toThrow(IsolationConflict);
    // Aborted + unwound → alice untouched.
    expect((db.prepare("SELECT balance FROM accounts WHERE name=?").get("alice") as { balance: number }).balance).toBe(100);
  });

  it("reading and writing the same key myself is not a self-conflict", async () => {
    const readWrite = tool<{ name: string; balance: number }>(
      "sql",
      (conn: SqliteDatabase, args) => {
        executeIsolated(conn, "SELECT balance FROM accounts WHERE name = ?", [args.name], {
          reads: [["accounts", args.name]],
        });
        executeIsolated(conn, "UPDATE accounts SET balance = ? WHERE name = ?", [args.balance, args.name], {
          writes: [["accounts", args.name]],
        });
        return { ok: true };
      },
      { name: "readWrite" },
    );
    const ctx = await agentTxn({ sql }, async () => {
      await readWrite({ name: "alice", balance: 70 });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect((db.prepare("SELECT balance FROM accounts WHERE name=?").get("alice") as { balance: number }).balance).toBe(70);
  });
});

// --- Serialize: the in-process registry coordination ------------------------

describe("Serialize registry coordination", () => {
  it("waitForBlockers blocks while a peer plans a conflicting write, resolves when it clears", async () => {
    const reg = new JournalRegistry();
    // A peer txn that plans to WRITE key ["x"].
    const peerEffect = new Effect({
      txnId: "peer",
      index: 0,
      tool: "w",
      args: {},
      resource: "sql",
      reversible: true,
    });
    peerEffect.writeKeys = [["sql", ["x"], 1]];
    const peer = { txnId: "peer", txn: { effects: [peerEffect] } };
    reg.register(peer);

    // I READ key ["x"] → my commit must wait on the peer.
    let resolved = false;
    const wait = reg
      .waitForBlockers("me", [["sql", ["x"], 0]], 5)
      .then(() => {
        resolved = true;
      });

    // Give the wait a few cycles; it must NOT resolve while the peer is open.
    await new Promise((r) => setTimeout(r, 30));
    expect(resolved).toBe(false);

    // Peer finishes → the wait proceeds.
    reg.unregister(peer);
    await wait;
    expect(resolved).toBe(true);
  });

  it("Serialize falls through to Abort if a conflict still stands after the wait", async () => {
    // No peers, but the diff still finds a moved key → Serialize.resolve throws.
    const policy = new Serialize(1);
    expect(() => policy.resolve(null, [{ resource: "sql", key: ["x"], versionAtRead: 0, versionNow: 1, versionExpected: 0 }])).toThrow(
      IsolationConflict,
    );
  });

  it("the runtime registers and unregisters agentTxns on the global registry", async () => {
    REGISTRY.clear();
    const before = ISOLATION_REGISTRY.openContexts().length;
    await agentTxn({}, async () => {
      // inside the txn we are registered
      expect(ISOLATION_REGISTRY.openContexts().length).toBe(before + 1);
    });
    // unregistered on finalisation
    expect(ISOLATION_REGISTRY.openContexts().length).toBe(before);
  });

  // Abort is the default policy — sanity that it raises on a conflict set.
  it("Abort raises IsolationConflict", () => {
    expect(() => new Abort().resolve(null, [{ resource: "r", key: ["k"], versionAtRead: 0, versionNow: 1, versionExpected: 0 }])).toThrow(
      IsolationConflict,
    );
  });
});

// --- on-disk: readsCommittedOnly path (meta-connection) ----------------------
//
// Mirrors tests/test_isolation_self_write.py on-disk section.
// The meta-connection (metaDb) sits outside the main connection's BEGIN so
// readVersion always reflects the latest committed state from any process,
// not the snapshot taken when the txn opened. This prevents the false
// self-conflict that would otherwise fire on on-disk read-then-write.

describe("SqliteAdapter with metaDb (readsCommittedOnly: true)", () => {
  let dir: string;
  let dbPath: string;
  let mainDb: Database.Database;
  let metaDb: Database.Database;
  let sql: SqliteAdapter;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "pherix-iso-"));
    dbPath = join(dir, "test.db");
    // WAL mode lets the meta-connection read committed state while the main
    // connection holds an open write transaction.
    mainDb = new Database(dbPath);
    mainDb.exec("PRAGMA journal_mode=WAL");
    mainDb.exec("CREATE TABLE counters (name TEXT PRIMARY KEY, val INTEGER)");
    mainDb.prepare("INSERT INTO counters VALUES (?, ?)").run("x", 0);
    metaDb = new Database(dbPath, { readonly: true });
    REGISTRY.clear();
    sql = new SqliteAdapter(mainDb, { metaDb });
  });

  afterEach(() => {
    mainDb.close();
    metaDb.close();
    rmSync(dir, { recursive: true, force: true });
  });

  it("readsCommittedOnly is true when metaDb is provided", () => {
    expect(sql.readsCommittedOnly()).toBe(true);
  });

  it("readsCommittedOnly is false without metaDb", () => {
    const mem = new Database(":memory:");
    const adapter = new SqliteAdapter(mem);
    expect(adapter.readsCommittedOnly()).toBe(false);
    mem.close();
  });

  it("read-then-write same key (no external writer) does not conflict", async () => {
    // Matrix #1 / THE BUG: without the meta-connection, readVersion inside a
    // BEGIN saw our own uncommitted version bump, making it look like a
    // cross-txn write — a false IsolationConflict. With metaDb the committed
    // base at read and at commit-time match, so the self-bump cancels.
    const readWrite = tool<{ val: number }>(
      "sql",
      (conn: SqliteDatabase, args) => {
        executeIsolated(
          conn,
          "SELECT val FROM counters WHERE name = ?",
          ["x"],
          { reads: [["counters", "x"]] },
        );
        executeIsolated(
          conn,
          "UPDATE counters SET val = ? WHERE name = ?",
          [args.val, "x"],
          { writes: [["counters", "x"]] },
        );
        return { ok: true };
      },
      { name: "readWrite_ondisk" },
    );

    const ctx = await agentTxn({ sql }, async () => {
      await readWrite({ val: 42 });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    const row = mainDb.prepare("SELECT val FROM counters WHERE name = ?").get("x") as { val: number };
    expect(row.val).toBe(42);
  });

  it("read-only key written by an external committed txn triggers a conflict", async () => {
    // Matrix #4: A reads x, B commits a write to x, A's meta-conn sees the
    // committed bump → IsolationConflict at A's commit.
    //
    // We use a nested agentTxn (like the Python test) so executeIsolated inside
    // writeB sees an active effect and bumps the version side-table correctly.
    const readA = tool<Record<string, never>>(
      "sql",
      (conn: SqliteDatabase, _args) =>
        executeIsolated(
          conn,
          "SELECT val FROM counters WHERE name = ?",
          ["x"],
          { reads: [["counters", "x"]] },
        ),
      { name: "readA_ondisk" },
    );

    const writeB = tool<{ val: number }>(
      "sql",
      (conn: SqliteDatabase, args) =>
        executeIsolated(
          conn,
          "UPDATE counters SET val = ? WHERE name = ?",
          [args.val, "x"],
          { writes: [["counters", "x"]] },
        ),
      { name: "writeB_ondisk" },
    );

    // db2: a second connection for the concurrent writer (no metaDb needed; it
    // commits and we discard it — cross-process detection is the metaDb's job).
    const db2 = new Database(dbPath);
    db2.exec("PRAGMA journal_mode=WAL");
    const sql2 = new SqliteAdapter(db2 as unknown as SqliteDatabase);

    await expect(
      agentTxn({ sql }, async () => {
        await readA({});
        // Nested agentTxn for the external write — the tool call sets activeEffect
        // so executeIsolated correctly bumps the version side-table and commits.
        await agentTxn({ sql: sql2 }, async () => {
          await writeB({ val: 99 });
        });
        // After this nested commit, the version of ("counters","x") is 1.
        // sql's metaDb is outside any BEGIN → sees version 1.
        // Our outer read_key has vAtRead=0 → conflict at A's commit.
      }),
    ).rejects.toThrow(IsolationConflict);

    db2.close();
  });
});
