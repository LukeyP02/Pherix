/**
 * RedisAdapter tests — mirror of tests/test_adapters_redis.py.
 *
 * Runs fully offline against an in-memory FakeRedis modelling DUMP/RESTORE/PTTL
 * faithfully: DUMP returns an opaque serialisation that preserves the value's
 * *type* (string or hash) and RESTORE rebuilds it exactly, so the type+TTL
 * preservation that motivates DUMP/RESTORE (over GET/SET) is genuinely tested.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { Effect } from "../src/index.js";
import {
  RedisAdapter,
  type RedisClient,
  type RedisPipeline,
} from "../src/adapters/index.js";

type Value =
  | { type: "string"; data: string }
  | { type: "hash"; data: Record<string, string> };

interface Entry {
  value: Value;
  expireAtMs: number | null; // wall-clock ms, or null for no expiry
}

/** A faithful-enough Redis fake: DUMP serialises {type,data} (preserving type),
 *  RESTORE rebuilds it, PTTL reports remaining ms (-1 none, -2 missing). */
class FakeRedis implements RedisClient {
  private store = new Map<string, Entry>();

  // --- adapter-facing surface (RedisClient) ---
  dump(key: string): Uint8Array | null {
    const e = this.store.get(key);
    if (e === undefined) return null;
    return new TextEncoder().encode(JSON.stringify(e.value));
  }

  pttl(key: string): number {
    const e = this.store.get(key);
    if (e === undefined) return -2;
    if (e.expireAtMs === null) return -1;
    return Math.max(0, e.expireAtMs - Date.now());
  }

  pipeline(): RedisPipeline {
    const ops: Array<() => void> = [];
    const self = this;
    return {
      del(key: string) {
        ops.push(() => self.store.delete(key));
      },
      restore(key: string, ttlMs: number, serialized: Uint8Array) {
        const value = JSON.parse(new TextDecoder().decode(serialized)) as Value;
        const expireAtMs = ttlMs > 0 ? Date.now() + ttlMs : null;
        ops.push(() => self.store.set(key, { value, expireAtMs }));
      },
      exec() {
        for (const op of ops) op();
        return [];
      },
    };
  }

  // --- tool-facing / test helpers ---
  set(key: string, data: string, exSeconds?: number): void {
    this.store.set(key, {
      value: { type: "string", data },
      expireAtMs: exSeconds ? Date.now() + exSeconds * 1000 : null,
    });
  }
  hset(key: string, data: Record<string, string>): void {
    this.store.set(key, { value: { type: "hash", data }, expireAtMs: null });
  }
  delete(key: string): void {
    this.store.delete(key);
  }
  get(key: string): string | null {
    const e = this.store.get(key);
    return e && e.value.type === "string" ? e.value.data : null;
  }
  hgetall(key: string): Record<string, string> | null {
    const e = this.store.get(key);
    return e && e.value.type === "hash" ? e.value.data : null;
  }
  type(key: string): string | null {
    return this.store.get(key)?.value.type ?? null;
  }
  exists(key: string): boolean {
    return this.store.has(key);
  }
  ttlSeconds(key: string): number {
    const ms = this.pttl(key);
    if (ms < 0) return ms;
    return Math.ceil(ms / 1000);
  }
}

function effect(args: Record<string, unknown>, index = 0): Effect {
  return new Effect({ txnId: "t", index, tool: "fake", args, resource: "redis", reversible: true });
}

let client: FakeRedis;
let adapter: RedisAdapter;

beforeEach(() => {
  client = new FakeRedis();
  adapter = new RedisAdapter(client);
});

describe("RedisAdapter", () => {
  it("is honestly reversible and named", () => {
    expect(adapter.supportsRollback()).toBe(true);
    expect(adapter.name).toBe("redis");
  });

  // --- left-inverse ---------------------------------------------------------
  it("restores a modified string to its original value", async () => {
    client.set("k", "original");
    const e = effect({ key: "k" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (c: FakeRedis, args: { key: string }) => c.set(args.key, "modified"));
    expect(client.get("k")).toBe("modified");
    await adapter.restore(e.snapshot);
    expect(client.get("k")).toBe("original");
  });

  it("deletes a created key on restore", async () => {
    const e = effect({ key: "new" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (c: FakeRedis, args: { key: string }) => c.set(args.key, "hello"));
    expect(client.exists("new")).toBe(true);
    await adapter.restore(e.snapshot);
    expect(client.exists("new")).toBe(false);
  });

  it("recreates a deleted pre-existing key on restore", async () => {
    client.set("keep", "precious");
    const e = effect({ key: "keep" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (c: FakeRedis, args: { key: string }) => c.delete(args.key));
    expect(client.exists("keep")).toBe(false);
    await adapter.restore(e.snapshot);
    expect(client.get("keep")).toBe("precious");
  });

  // --- type + TTL preservation (why DUMP/RESTORE, not GET/SET) --------------
  it("preserves the hash value type on restore", async () => {
    client.hset("h", { a: "1", b: "2" });
    const e = effect({ key: "h" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (c: FakeRedis, args: { key: string }) => {
      c.delete(args.key);
      c.set(args.key, "now-a-string"); // wrong type
    });
    await adapter.restore(e.snapshot);
    expect(client.type("h")).toBe("hash");
    expect(client.hgetall("h")).toEqual({ a: "1", b: "2" });
  });

  it("restores the TTL", async () => {
    client.set("expiring", "v0", 1000); // 1000s TTL
    const e = effect({ key: "expiring" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (c: FakeRedis, args: { key: string }) => c.set(args.key, "v1")); // clears TTL
    expect(client.ttlSeconds("expiring")).toBe(-1);
    await adapter.restore(e.snapshot);
    expect(client.get("expiring")).toBe("v0");
    expect(client.ttlSeconds("expiring")).toBeGreaterThan(0);
    expect(client.ttlSeconds("expiring")).toBeLessThanOrEqual(1000);
  });

  it("restores all keys in a multi-key effect", async () => {
    client.set("a", "a0");
    client.set("b", "b0");
    const e = effect({ keys: ["a", "b", "c"] });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (c: FakeRedis) => {
      c.set("a", "a1");
      c.delete("b");
      c.set("c", "c1");
    });
    await adapter.restore(e.snapshot);
    expect(client.get("a")).toBe("a0");
    expect(client.get("b")).toBe("b0");
    expect(client.exists("c")).toBe(false);
  });

  // --- partial failure ------------------------------------------------------
  it("partial failure: tool mutates then throws, restore lands every captured key", async () => {
    client.set("x", "x0");
    client.set("y", "y0");
    const e = effect({ keys: ["x", "y"] });
    e.snapshot = await adapter.snapshot(e);
    expect(() =>
      adapter.apply(e, (c: FakeRedis) => {
        c.set("x", "x1");
        throw new Error("boom mid-effect");
      }),
    ).toThrow("boom");
    await adapter.restore(e.snapshot);
    expect(client.get("x")).toBe("x0");
    expect(client.get("y")).toBe("y0");
  });

  it("payload is JSON-serialisable", async () => {
    client.set("p", "raw");
    const e = effect({ keys: ["p", "absent"] });
    e.snapshot = await adapter.snapshot(e);
    expect(() => JSON.stringify(e.snapshot!.payload)).not.toThrow();
  });

  it("injects the client as the first arg", async () => {
    const e = effect({ key: "z" });
    e.snapshot = await adapter.snapshot(e);
    const seen: Record<string, unknown> = {};
    await adapter.apply(e, (c: FakeRedis, args: { key: string }) => {
      seen["client"] = c;
      seen["key"] = args.key;
    });
    expect(seen["client"]).toBe(client);
    expect(seen["key"]).toBe("z");
  });

  it("captures nothing when the effect touches no key", async () => {
    const e = effect({ unrelated: "value" });
    e.snapshot = await adapter.snapshot(e);
    expect(e.snapshot.payload["keys"]).toEqual({});
    await expect(adapter.restore(e.snapshot)).resolves.toBeUndefined();
  });
});
