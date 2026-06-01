/** Mirrors tests/test_adapters_memory.py — governed agent memory as adapter +
 *  policy. Proves the north-star claim that governed memory is NOT a new axis:
 *  remember/recall/forget drop onto the existing ResourceAdapter protocol with
 *  no engine surgery. Journalled effects, rollback with the txn, recall sees
 *  only committed state, content-addressed versioning, namespacing, durable
 *  round-trip across a fresh adapter, and a dry-run structural diff. */

import { createHash } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  EffectStatus,
  MemoryAdapter,
  type MemoryHandle,
  REGISTRY,
  TxnState,
  agentTxn,
  dryRun,
  tool,
  type SqliteDatabase,
} from "../src/index.js";

let db: Database.Database;
let mem: MemoryAdapter;

beforeEach(() => {
  REGISTRY.clear();
  db = new Database(":memory:");
  mem = new MemoryAdapter(db);
});

afterEach(() => {
  db.close();
});

/** The standard remember / recall / forget tool trio for one test. */
function memTools() {
  return {
    remember: tool<{ key: string; value: string }>(
      "memory",
      (h: MemoryHandle, args) => {
        h.remember(args.key, args.value);
        return { ok: true };
      },
      { name: "remember" },
    ),
    recall: tool<{ key: string }>("memory", (h: MemoryHandle, args) => h.recall(args.key), {
      name: "recall",
    }),
    forget: tool<{ key: string }>(
      "memory",
      (h: MemoryHandle, args) => {
        h.forget(args.key);
        return { ok: true };
      },
      { name: "forget" },
    ),
  };
}

function stored(adapter: MemoryAdapter, namespace = "default"): Record<string, string> {
  const rows = (adapter.connection as SqliteDatabase)
    .prepare("SELECT mem_key, value FROM _pherix_memory WHERE namespace = ?")
    .all(namespace) as Array<{ mem_key: string; value: string }>;
  const out: Record<string, string> = {};
  for (const r of rows) out[r.mem_key] = r.value;
  return out;
}

describe("MemoryAdapter — effects land in the journal", () => {
  it("remember / recall / forget are reversible memory effects, APPLIED on commit", async () => {
    const t = memTools();
    const ctx = await agentTxn({ memory: mem }, async () => {
      await t.remember({ key: "city", value: "London" });
      expect(await t.recall({ key: "city" })).toBe("London");
      await t.forget({ key: "city" });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(ctx.txn.effects.map((e) => e.tool)).toEqual(["remember", "recall", "forget"]);
    expect(ctx.txn.effects.every((e) => e.resource === "memory")).toBe(true);
    expect(ctx.txn.effects.every((e) => e.reversible)).toBe(true);
    expect(ctx.txn.effects.every((e) => e.status === EffectStatus.APPLIED)).toBe(true);
  });
});

describe("MemoryAdapter — rollback with the transaction", () => {
  it("a rolled-back remember simply never happened", async () => {
    const t = memTools();
    await agentTxn({ memory: mem }, async (ctx) => {
      await t.remember({ key: "city", value: "London" });
      await ctx.rollback();
    });
    expect(stored(mem)).toEqual({});
  });

  it("an exception in the block rolls memory back", async () => {
    const t = memTools();
    await expect(
      agentTxn({ memory: mem }, async () => {
        await t.remember({ key: "city", value: "London" });
        throw new Error("boom");
      }),
    ).rejects.toThrow(/boom/);
    expect(stored(mem)).toEqual({});
  });

  it("recall sees committed memory but not a rolled-back write", async () => {
    const t = memTools();
    await agentTxn({ memory: mem }, async () => {
      await t.remember({ key: "city", value: "London" });
    });
    await agentTxn({ memory: mem }, async () => {
      expect(await t.recall({ key: "city" })).toBe("London");
    });

    await agentTxn({ memory: mem }, async (ctx) => {
      await t.remember({ key: "city", value: "Paris" });
      await ctx.rollback();
    });
    await agentTxn({ memory: mem }, async () => {
      // the rolled-back overwrite never happened — the committed value stands
      expect(await t.recall({ key: "city" })).toBe("London");
    });
  });

  it("forget rolls back too — the value is restored", async () => {
    const t = memTools();
    await agentTxn({ memory: mem }, async () => {
      await t.remember({ key: "city", value: "London" });
    });
    await agentTxn({ memory: mem }, async (ctx) => {
      await t.forget({ key: "city" });
      await ctx.rollback();
    });
    await agentTxn({ memory: mem }, async () => {
      expect(await t.recall({ key: "city" })).toBe("London");
    });
  });
});

describe("MemoryAdapter — recall is read-only by construction", () => {
  it("recall records a read-key and no write-key; remember records a write-key", async () => {
    const t = memTools();
    const ctx = await agentTxn({ memory: mem }, async () => {
      await t.remember({ key: "city", value: "London" });
      await t.recall({ key: "city" });
    });
    const byTool = new Map(ctx.txn.effects.map((e) => [e.tool, e]));
    const recall = byTool.get("recall")!;
    expect(recall.readKeys.length).toBeGreaterThan(0);
    expect(recall.writeKeys).toEqual([]);
    expect(byTool.get("remember")!.writeKeys.length).toBeGreaterThan(0);
  });
});

describe("MemoryAdapter — content-addressed versioning", () => {
  it("readVersion is __missing__ for an absent key and the sha256 of the value once set", async () => {
    expect(mem.readVersion(["k"])).toBe("__missing__");
    const t = memTools();
    await agentTxn({ memory: mem }, async () => {
      await t.remember({ key: "k", value: "hello" });
    });
    const expected = createHash("sha256").update("hello", "utf8").digest("hex");
    expect(mem.readVersion(["k"])).toBe(expected);
    expect(mem.writeVersion(["k"])).toBe(expected);
  });

  it("rejects a multi-element version key", () => {
    expect(() => mem.readVersion(["a", "b"])).toThrow(/1-tuple/);
  });
});

describe("MemoryAdapter — namespacing isolates two agents", () => {
  it("two namespaces on one connection do not collide", async () => {
    const t = memTools();
    const a = new MemoryAdapter(db, { namespace: "agent-a" });
    const b = new MemoryAdapter(db, { namespace: "agent-b" });
    await agentTxn({ memory: a }, async () => {
      await t.remember({ key: "k", value: "from-a" });
    });
    await agentTxn({ memory: b }, async () => {
      await t.remember({ key: "k", value: "from-b" });
      expect(await t.recall({ key: "k" })).toBe("from-b");
    });
    await agentTxn({ memory: a }, async () => {
      expect(await t.recall({ key: "k" })).toBe("from-a");
    });
  });
});

describe("MemoryAdapter — durability across a fresh adapter", () => {
  it("committed memory survives a fresh adapter; a rolled-back write does not", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "pherix_mem_test_"));
    const file = path.join(dir, "memory.db");
    try {
      const t = memTools();
      const conn1 = new Database(file);
      await agentTxn({ memory: new MemoryAdapter(conn1) }, async () => {
        await t.remember({ key: "city", value: "Berlin" });
      });
      await agentTxn({ memory: new MemoryAdapter(conn1) }, async (ctx) => {
        await t.remember({ key: "ghost", value: "vanishes" });
        await ctx.rollback();
      });
      conn1.close();

      // A brand-new connection + adapter on the same file recalls the committed
      // value and never sees the rolled-back one — durability is the SQLite file
      // persisting committed state across runs.
      const conn2 = new Database(file);
      await agentTxn({ memory: new MemoryAdapter(conn2) }, async () => {
        expect(await t.recall({ key: "city" })).toBe("Berlin");
        expect(await t.recall({ key: "ghost" })).toBeNull();
      });
      conn2.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("MemoryAdapter — dry-run structural diff (StateDiffable)", () => {
  it("reports a memory state diff and discards the write", async () => {
    const t = memTools();
    const ctx = await dryRun({ memory: mem }, async () => {
      await t.remember({ key: "city", value: "Oslo" });
    });
    const diff = (ctx.result!.stateDiff as Record<string, Record<string, string[]>>)["memory"]!;
    expect(diff.keys_added).toEqual(["city"]);
    // the dry-run discarded the write
    expect(stored(mem)).toEqual({});
  });
});
