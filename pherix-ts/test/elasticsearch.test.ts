/**
 * ElasticsearchAdapter — mirror of tests/test_adapters_elasticsearch.py.
 *
 * Fully offline via a faithful in-memory fake ES client — a genuine snapshot
 * -> mutate -> restore round-trip against documents. `refresh` is accepted and
 * ignored (the fake is synchronous, so writes are immediately visible).
 */

import { describe, expect, it } from "vitest";
import { ElasticsearchAdapter, type EsClient } from "../src/index.js";
import { Effect } from "../src/effects.js";

const INDEX = "pherix-test-index";

class FakeEsClient implements EsClient {
  private indices = new Map<string, Map<string, Record<string, unknown>>>();
  private idx(index: string): Map<string, Record<string, unknown>> {
    let store = this.indices.get(index);
    if (store === undefined) {
      store = new Map();
      this.indices.set(index, store);
    }
    return store;
  }
  async exists(params: { index: string; id: string }): Promise<boolean> {
    return this.idx(params.index).has(params.id);
  }
  async get(params: { index: string; id: string }): Promise<{ _source: Record<string, unknown> }> {
    return { _source: structuredClone(this.idx(params.index).get(params.id)!) };
  }
  async index(params: { index: string; id: string; document: Record<string, unknown> }): Promise<unknown> {
    this.idx(params.index).set(params.id, structuredClone(params.document));
    return { result: "created" };
  }
  async delete(params: { index: string; id: string }): Promise<unknown> {
    this.idx(params.index).delete(params.id);
    return { result: "deleted" };
  }
}

function makeEffect(args: Record<string, unknown>, index = 0): Effect {
  return new Effect({ txnId: "t", index, tool: "fake", args, resource: "elasticsearch", reversible: true });
}

async function get(es: FakeEsClient, docId: string): Promise<Record<string, unknown> | null> {
  if (!(await es.exists({ index: INDEX, id: docId }))) return null;
  return (await es.get({ index: INDEX, id: docId }))._source;
}
async function put(es: FakeEsClient, docId: string, value: string): Promise<void> {
  await es.index({ index: INDEX, id: docId, document: { v: value } });
}

function fresh(): { es: FakeEsClient; adapter: ElasticsearchAdapter } {
  const es = new FakeEsClient();
  return { es, adapter: new ElasticsearchAdapter(es, INDEX) };
}

describe("ElasticsearchAdapter", () => {
  it("is honest: supportsRollback() is true", () => {
    expect(fresh().adapter.supportsRollback()).toBe(true);
    expect(fresh().adapter.name).toBe("elasticsearch");
  });

  // --- left-inverse ---

  it("restores a modified document to its original (left-inverse)", async () => {
    const { es, adapter } = fresh();
    await put(es, "doc", "original");
    const effect = makeEffect({ key: "doc" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeEsClient, args: { key: string }) => {
      await put(client, args.key, "modified");
    });
    expect(await get(es, "doc")).toEqual({ v: "modified" });
    await adapter.restore(effect.snapshot);
    expect(await get(es, "doc")).toEqual({ v: "original" });
  });

  it("deletes a created document on restore", async () => {
    const { es, adapter } = fresh();
    const effect = makeEffect({ key: "new" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeEsClient, args: { key: string }) => {
      await put(client, args.key, "hello");
    });
    await adapter.restore(effect.snapshot);
    expect(await get(es, "new")).toBeNull();
  });

  it("recreates a deleted pre-existing document on restore", async () => {
    const { es, adapter } = fresh();
    await put(es, "keep", "precious");
    const effect = makeEffect({ key: "keep" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeEsClient, args: { key: string }) => {
      await client.delete({ index: INDEX, id: args.key, refresh: true });
    });
    await adapter.restore(effect.snapshot);
    expect(await get(es, "keep")).toEqual({ v: "precious" });
  });

  it("restores all keys of a multi-key effect", async () => {
    const { es, adapter } = fresh();
    await put(es, "a", "a0");
    await put(es, "b", "b0");
    const effect = makeEffect({ keys: ["a", "b", "c"] });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeEsClient) => {
      await put(client, "a", "a1");
      await client.delete({ index: INDEX, id: "b", refresh: true });
      await put(client, "c", "c1");
    });
    await adapter.restore(effect.snapshot);
    expect(await get(es, "a")).toEqual({ v: "a0" });
    expect(await get(es, "b")).toEqual({ v: "b0" });
    expect(await get(es, "c")).toBeNull();
  });

  // --- partial failure ---

  it("restores captured keys even when apply raises mid-effect (partial failure)", async () => {
    const { es, adapter } = fresh();
    await put(es, "x", "x0");
    await put(es, "y", "y0");
    const effect = makeEffect({ keys: ["x", "y"] });
    effect.snapshot = await adapter.snapshot(effect);
    await expect(
      adapter.apply(effect, async (client: FakeEsClient) => {
        await put(client, "x", "x1");
        throw new Error("boom mid-effect");
      }),
    ).rejects.toThrow("boom");
    await adapter.restore(effect.snapshot);
    expect(await get(es, "x")).toEqual({ v: "x0" });
    expect(await get(es, "y")).toEqual({ v: "y0" });
  });

  it("payload is JSON-serialisable", async () => {
    const { es, adapter } = fresh();
    await put(es, "p", "v");
    const effect = makeEffect({ keys: ["p", "absent"] });
    const handle = await adapter.snapshot(effect);
    expect(() => JSON.stringify(handle.payload)).not.toThrow();
  });

  it("deep-copies the source so a later live mutation cannot alter the snapshot", async () => {
    const { es, adapter } = fresh();
    await put(es, "d", "before");
    const effect = makeEffect({ key: "d" });
    const handle = await adapter.snapshot(effect);
    await put(es, "d", "after");
    expect((handle.payload["docs"] as Record<string, { doc: unknown }>)["d"]!.doc).toEqual({ v: "before" });
  });

  it("injects the client as the tool's first arg", async () => {
    const { es, adapter } = fresh();
    const effect = makeEffect({ key: "z" });
    effect.snapshot = await adapter.snapshot(effect);
    let seen: { client: unknown; key: string } | undefined;
    await adapter.apply(effect, (client: FakeEsClient, args: { key: string }) => {
      seen = { client, key: args.key };
    });
    expect(seen!.client).toBe(es);
    expect(seen!.key).toBe("z");
  });

  it("snapshots nothing when the effect names no document", async () => {
    const { adapter } = fresh();
    const effect = makeEffect({ unrelated: "value" });
    const handle = await adapter.snapshot(effect);
    expect(handle.payload["docs"]).toEqual({});
    await expect(adapter.restore(handle)).resolves.toBeUndefined();
  });
});
