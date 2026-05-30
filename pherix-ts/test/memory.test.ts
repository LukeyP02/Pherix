/**
 * MemoryAdapter tests — mirror of tests/test_adapters_memory.py.
 *
 * Runs fully offline against an in-memory better-sqlite3 database. Covers the
 * core contract: journalled effects, rollback-via-savepoint, content-addressed
 * versioning, and the dry-run state diff.
 */

import Database from "better-sqlite3";
import { beforeEach, describe, expect, it } from "vitest";
import {
  Effect,
  EffectStatus,
  MemoryAdapter,
  MemoryHandle,
  REGISTRY,
  TxnState,
  agentTxn,
  tool,
} from "../src/index.js";
import type { SqliteDatabase } from "../src/adapters/index.js";

function makeEffect(index: number = 0): Effect {
  return new Effect({
    txnId: "txn-test",
    index,
    tool: "remember_fact",
    args: { key: "k", value: "v" },
    resource: "memory",
    reversible: true,
  });
}

describe("MemoryAdapter — basic operations", () => {
  let db: Database.Database;
  let adapter: MemoryAdapter;

  beforeEach(() => {
    db = new Database(":memory:");
    adapter = new MemoryAdapter(db as unknown as SqliteDatabase);
  });

  it("remember / recall round-trip outside agentTxn", () => {
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    handle.remember("k", "hello");
    expect(handle.recall("k")).toBe("hello");
  });

  it("recall returns null for absent key", () => {
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    expect(handle.recall("missing")).toBeNull();
  });

  it("forget removes a key", () => {
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    handle.remember("k", "v");
    handle.forget("k");
    expect(handle.recall("k")).toBeNull();
  });

  it("non-string value is JSON-encoded", () => {
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    handle.remember("obj", { a: 1 });
    expect(handle.recall("obj")).toBe('{"a":1}');
  });
});

describe("MemoryAdapter — versioning", () => {
  let db: Database.Database;
  let adapter: MemoryAdapter;

  beforeEach(() => {
    db = new Database(":memory:");
    adapter = new MemoryAdapter(db as unknown as SqliteDatabase);
  });

  it("absent key returns __missing__", () => {
    expect(adapter.readVersion(["absent"])).toBe("__missing__");
  });

  it("version changes after remember", () => {
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    const before = adapter.readVersion(["k"]);
    handle.remember("k", "hello");
    const after = adapter.readVersion(["k"]);
    expect(before).toBe("__missing__");
    expect(after).not.toBe("__missing__");
    expect(after).toHaveLength(64); // sha256 hex
  });

  it("writeVersion matches readVersion after write", () => {
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    handle.remember("k", "hello");
    expect(adapter.readVersion(["k"])).toBe(adapter.writeVersion(["k"]));
  });
});

describe("MemoryAdapter — savepoint rollback", () => {
  let db: Database.Database;
  let adapter: MemoryAdapter;

  beforeEach(() => {
    db = new Database(":memory:");
    adapter = new MemoryAdapter(db as unknown as SqliteDatabase);
  });

  it("restore rolls back a remember", () => {
    adapter.begin();
    const effect = makeEffect(0);
    const handle = adapter.snapshot(effect);
    const mh = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    mh.remember("k", "hello");
    expect(mh.recall("k")).toBe("hello");
    adapter.restore(handle);
    expect(mh.recall("k")).toBeNull();
    adapter.rollback();
  });

  it("commit persists the remember", () => {
    adapter.begin();
    const effect = makeEffect(0);
    adapter.snapshot(effect);
    const mh = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    mh.remember("k", "committed");
    adapter.commit();
    expect(mh.recall("k")).toBe("committed");
  });
});

describe("MemoryAdapter — isolation key recording", () => {
  it("remember records a write_key into the effect", () => {
    const db = new Database(":memory:");
    const adapter = new MemoryAdapter(db as unknown as SqliteDatabase);
    const effect = makeEffect(0);
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", effect, adapter);
    handle.remember("greeting", "hello");
    expect(effect.writeKeys).toHaveLength(1);
    const [resource, key, version] = effect.writeKeys[0];
    expect(resource).toBe("memory");
    expect(key).toEqual(["greeting"]);
    expect(typeof version).toBe("string");
    expect(version).not.toBe("__missing__");
  });

  it("recall records a read_key (deduped)", () => {
    const db = new Database(":memory:");
    const adapter = new MemoryAdapter(db as unknown as SqliteDatabase);
    const effect = makeEffect(0);
    const handle = new MemoryHandle(db as unknown as SqliteDatabase, "default", effect, adapter);
    handle.remember("k", "v");
    // Clear write keys to isolate the recall read below.
    effect.writeKeys.length = 0;
    handle.recall("k");
    handle.recall("k"); // second call should be deduped
    expect(effect.readKeys).toHaveLength(1);
    const [resource, key] = effect.readKeys[0];
    expect(resource).toBe("memory");
    expect(key).toEqual(["k"]);
  });
});

describe("MemoryAdapter — agentTxn integration", () => {
  beforeEach(() => {
    REGISTRY.clear();
  });

  it("remember commits correctly", async () => {
    const db = new Database(":memory:");
    const mem = new MemoryAdapter(db as unknown as SqliteDatabase);

    const rememberFact = tool<{ key: string; value: string }>(
      "memory",
      (handle: MemoryHandle, args: { key: string; value: string }) => {
        handle.remember(args.key, args.value);
        return { ok: true };
      },
      { name: "rememberFact" },
    );

    const ctx = await agentTxn({ memory: mem }, async () => {
      await rememberFact({ key: "greeting", value: "hello" });
    });

    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(ctx.txn.effects).toHaveLength(1);
    expect(ctx.txn.effects[0].status).toBe(EffectStatus.APPLIED);

    // The remembered value persists after commit.
    const mh = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    expect(mh.recall("greeting")).toBe("hello");
  });

  it("rollback via error leaves no trace", async () => {
    const db = new Database(":memory:");
    const mem = new MemoryAdapter(db as unknown as SqliteDatabase);

    const rememberFact = tool<{ key: string; value: string }>(
      "memory",
      (handle: MemoryHandle, args: { key: string; value: string }) => {
        handle.remember(args.key, args.value);
        return { ok: true };
      },
      { name: "rememberFactRollback" },
    );

    try {
      await agentTxn({ memory: mem }, async () => {
        await rememberFact({ key: "temp", value: "gone" });
        throw new Error("abort");
      });
    } catch {
      // expected
    }

    // The rolled-back remember must not have persisted.
    const mh = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);
    expect(mh.recall("temp")).toBeNull();
  });
});

describe("MemoryAdapter — state diff (dry-run)", () => {
  it("stateBaseline / stateDiff reflects added, modified, deleted", () => {
    const db = new Database(":memory:");
    const adapter = new MemoryAdapter(db as unknown as SqliteDatabase);
    const mh = new MemoryHandle(db as unknown as SqliteDatabase, "default", null, null);

    // Commit some baseline state outside a txn (raw SQLite, no savepoints needed
    // for this structural test).
    adapter.begin();
    mh.remember("a", "v1"); // will be modified
    mh.remember("b", "v1"); // will be deleted
    adapter.commit();

    // Capture baseline, then mutate inside a txn so stateDiff sees the changes.
    adapter.begin();
    const baseline = adapter.stateBaseline() as Record<string, string>;
    expect(Object.keys(baseline).sort()).toEqual(["a", "b"]);

    mh.remember("a", "v2"); // modify
    mh.remember("c", "v3"); // add
    mh.forget("b");          // delete

    const diff = adapter.stateDiff(baseline) as {
      keys_added: string[];
      keys_modified: string[];
      keys_deleted: string[];
    };
    expect(diff.keys_added).toContain("c");
    expect(diff.keys_modified).toContain("a");
    expect(diff.keys_deleted).toContain("b");

    adapter.rollback(); // discard the mutations
    db.close();
  });
});
