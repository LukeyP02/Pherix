/**
 * DynamoDbAdapter — mirror of tests/test_adapters_dynamodb.py.
 *
 * Fully offline via a faithful in-memory fake DynamoDB client (low-level typed
 * item form) — a genuine snapshot -> mutate -> restore round-trip against items.
 */

import { describe, expect, it } from "vitest";
import { DynamoDbAdapter, type DynamoDbClient } from "../src/index.js";
import { Effect } from "../src/effects.js";

const TABLE = "pherix-test-table";

/** In-memory low-level DynamoDB double: items keyed by pk string -> typed item. */
class FakeDynamoDb implements DynamoDbClient {
  private items = new Map<string, Record<string, unknown>>();
  constructor(private readonly keyAttr = "pk") {}

  private pk(key: Record<string, unknown>): string {
    return (key[this.keyAttr] as { S: string }).S;
  }

  async getItem(params: { TableName: string; Key: Record<string, unknown> }): Promise<{ Item?: Record<string, unknown> }> {
    const item = this.items.get(this.pk(params.Key));
    return item === undefined ? {} : { Item: structuredClone(item) };
  }

  async putItem(params: { TableName: string; Item: Record<string, unknown> }): Promise<unknown> {
    const k = (params.Item[this.keyAttr] as { S: string }).S;
    this.items.set(k, structuredClone(params.Item));
    return {};
  }

  async deleteItem(params: { TableName: string; Key: Record<string, unknown> }): Promise<unknown> {
    this.items.delete(this.pk(params.Key));
    return {};
  }
}

function makeEffect(args: Record<string, unknown>, index = 0): Effect {
  return new Effect({ txnId: "t", index, tool: "fake", args, resource: "dynamodb", reversible: true });
}

async function get(ddb: FakeDynamoDb, key: string): Promise<string | null> {
  const resp = await ddb.getItem({ TableName: TABLE, Key: { pk: { S: key } } });
  return resp.Item === undefined ? null : (resp.Item["v"] as { S: string }).S;
}
async function put(ddb: FakeDynamoDb, key: string, value: string): Promise<void> {
  await ddb.putItem({ TableName: TABLE, Item: { pk: { S: key }, v: { S: value } } });
}

function fresh(): { ddb: FakeDynamoDb; adapter: DynamoDbAdapter } {
  const ddb = new FakeDynamoDb();
  return { ddb, adapter: new DynamoDbAdapter(ddb, TABLE) };
}

describe("DynamoDbAdapter", () => {
  it("is honest: supportsRollback() is true", () => {
    expect(fresh().adapter.supportsRollback()).toBe(true);
    expect(fresh().adapter.name).toBe("dynamodb");
  });

  // --- left-inverse: snapshot -> apply -> restore returns to before-image ---

  it("restores a modified item to its original (left-inverse)", async () => {
    const { ddb, adapter } = fresh();
    await put(ddb, "doc", "original");
    const effect = makeEffect({ key: "doc" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeDynamoDb, args: { key: string }) => {
      await put(client, args.key, "modified");
    });
    expect(await get(ddb, "doc")).toBe("modified");
    await adapter.restore(effect.snapshot);
    expect(await get(ddb, "doc")).toBe("original");
  });

  it("deletes a created item on restore", async () => {
    const { ddb, adapter } = fresh();
    const effect = makeEffect({ key: "new" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeDynamoDb, args: { key: string }) => {
      await put(client, args.key, "hello");
    });
    await adapter.restore(effect.snapshot);
    expect(await get(ddb, "new")).toBeNull();
  });

  it("recreates a deleted pre-existing item on restore", async () => {
    const { ddb, adapter } = fresh();
    await put(ddb, "keep", "precious");
    const effect = makeEffect({ key: "keep" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeDynamoDb, args: { key: string }) => {
      await client.deleteItem({ TableName: TABLE, Key: { pk: { S: args.key } } });
    });
    await adapter.restore(effect.snapshot);
    expect(await get(ddb, "keep")).toBe("precious");
  });

  it("restores all keys of a multi-key effect", async () => {
    const { ddb, adapter } = fresh();
    await put(ddb, "a", "a0");
    await put(ddb, "b", "b0");
    const effect = makeEffect({ keys: ["a", "b", "c"] });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeDynamoDb) => {
      await put(client, "a", "a1");
      await client.deleteItem({ TableName: TABLE, Key: { pk: { S: "b" } } });
      await put(client, "c", "c1");
    });
    await adapter.restore(effect.snapshot);
    expect(await get(ddb, "a")).toBe("a0");
    expect(await get(ddb, "b")).toBe("b0");
    expect(await get(ddb, "c")).toBeNull();
  });

  // --- partial failure: apply raises mid-effect, restore still lands every key ---

  it("restores captured keys even when apply raises mid-effect (partial failure)", async () => {
    const { ddb, adapter } = fresh();
    await put(ddb, "x", "x0");
    await put(ddb, "y", "y0");
    const effect = makeEffect({ keys: ["x", "y"] });
    effect.snapshot = await adapter.snapshot(effect);
    await expect(
      adapter.apply(effect, async (client: FakeDynamoDb) => {
        await put(client, "x", "x1");
        throw new Error("boom mid-effect");
      }),
    ).rejects.toThrow("boom");
    await adapter.restore(effect.snapshot);
    expect(await get(ddb, "x")).toBe("x0");
    expect(await get(ddb, "y")).toBe("y0");
  });

  it("payload is JSON-serialisable", async () => {
    const { ddb, adapter } = fresh();
    await put(ddb, "p", "v");
    const effect = makeEffect({ keys: ["p", "absent"] });
    const handle = await adapter.snapshot(effect);
    expect(() => JSON.stringify(handle.payload)).not.toThrow();
  });

  it("injects the client as the tool's first arg", async () => {
    const { ddb, adapter } = fresh();
    const effect = makeEffect({ key: "z" });
    effect.snapshot = await adapter.snapshot(effect);
    let seen: { client: unknown; key: string } | undefined;
    await adapter.apply(effect, (client: FakeDynamoDb, args: { key: string }) => {
      seen = { client, key: args.key };
    });
    expect(seen!.client).toBe(ddb);
    expect(seen!.key).toBe("z");
  });

  it("snapshots nothing when the effect names no item", async () => {
    const { adapter } = fresh();
    const effect = makeEffect({ unrelated: "value" });
    const handle = await adapter.snapshot(effect);
    expect(handle.payload["items"]).toEqual({});
    await expect(adapter.restore(handle)).resolves.toBeUndefined();
  });

  it("addresses a custom key attribute", async () => {
    const ddb = new FakeDynamoDb("id");
    const adapter = new DynamoDbAdapter(ddb, "custom", { keyAttr: "id" });
    await ddb.putItem({ TableName: "custom", Item: { id: { S: "k" }, v: { S: "0" } } });
    const effect = makeEffect({ key: "k" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (c: FakeDynamoDb) => {
      await c.putItem({ TableName: "custom", Item: { id: { S: "k" }, v: { S: "1" } } });
    });
    await adapter.restore(effect.snapshot);
    const resp = await ddb.getItem({ TableName: "custom", Key: { id: { S: "k" } } });
    expect((resp.Item!["v"] as { S: string }).S).toBe("0");
  });
});
