/**
 * GcsAdapter — mirror of tests/test_adapters_gcs.py.
 *
 * Fully offline via a faithful in-memory fake GCS client — a genuine snapshot
 * -> mutate -> restore round-trip against blob bytes.
 */

import { describe, expect, it } from "vitest";
import { GcsAdapter, type GcsBlob, type GcsBucket, type GcsClient } from "../src/index.js";
import { Effect } from "../src/effects.js";

const BUCKET = "pherix-test-bucket";

class FakeGcsClient implements GcsClient {
  private buckets = new Map<string, Map<string, Buffer>>();
  bucket(name: string): GcsBucket {
    let store = this.buckets.get(name);
    if (store === undefined) {
      store = new Map();
      this.buckets.set(name, store);
    }
    return new FakeBucket(store);
  }
}

class FakeBucket implements GcsBucket {
  constructor(private readonly store: Map<string, Buffer>) {}
  file(name: string): GcsBlob {
    return new FakeBlob(this.store, name);
  }
}

class FakeBlob implements GcsBlob {
  constructor(private readonly store: Map<string, Buffer>, private readonly name: string) {}
  async exists(): Promise<[boolean]> {
    return [this.store.has(this.name)];
  }
  async download(): Promise<[Buffer]> {
    return [Buffer.from(this.store.get(this.name)!)];
  }
  async save(data: Buffer | Uint8Array): Promise<unknown> {
    this.store.set(this.name, Buffer.from(data));
    return undefined;
  }
  async delete(): Promise<unknown> {
    this.store.delete(this.name);
    return undefined;
  }
}

function makeEffect(args: Record<string, unknown>, index = 0): Effect {
  return new Effect({ txnId: "t", index, tool: "fake", args, resource: "gcs", reversible: true });
}

async function get(gcs: FakeGcsClient, key: string): Promise<Buffer | null> {
  const blob = gcs.bucket(BUCKET).file(key);
  const [present] = await blob.exists();
  if (!present) return null;
  const [body] = await blob.download();
  return body;
}
async function put(gcs: FakeGcsClient, key: string, body: Buffer): Promise<void> {
  await gcs.bucket(BUCKET).file(key).save(body);
}

function fresh(): { gcs: FakeGcsClient; adapter: GcsAdapter } {
  const gcs = new FakeGcsClient();
  return { gcs, adapter: new GcsAdapter(gcs, BUCKET) };
}

describe("GcsAdapter", () => {
  it("is honest: supportsRollback() is true", () => {
    expect(fresh().adapter.supportsRollback()).toBe(true);
    expect(fresh().adapter.name).toBe("gcs");
  });

  // --- left-inverse ---

  it("restores modified blob to its original bytes (left-inverse)", async () => {
    const { gcs, adapter } = fresh();
    await put(gcs, "doc.bin", Buffer.from("original"));
    const effect = makeEffect({ key: "doc.bin" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeGcsClient, args: { key: string }) => {
      await put(client, args.key, Buffer.from("modified"));
    });
    expect((await get(gcs, "doc.bin"))?.toString()).toBe("modified");
    await adapter.restore(effect.snapshot);
    expect((await get(gcs, "doc.bin"))?.toString()).toBe("original");
  });

  it("deletes a created blob on restore", async () => {
    const { gcs, adapter } = fresh();
    const effect = makeEffect({ key: "new.bin" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeGcsClient, args: { key: string }) => {
      await put(client, args.key, Buffer.from("hello"));
    });
    await adapter.restore(effect.snapshot);
    expect(await get(gcs, "new.bin")).toBeNull();
  });

  it("recreates a deleted pre-existing blob on restore", async () => {
    const { gcs, adapter } = fresh();
    await put(gcs, "keep.bin", Buffer.from("precious"));
    const effect = makeEffect({ key: "keep.bin" });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeGcsClient, args: { key: string }) => {
      await client.bucket(BUCKET).file(args.key).delete();
    });
    await adapter.restore(effect.snapshot);
    expect((await get(gcs, "keep.bin"))?.toString()).toBe("precious");
  });

  it("restores all keys of a multi-key effect", async () => {
    const { gcs, adapter } = fresh();
    await put(gcs, "a", Buffer.from("a0"));
    await put(gcs, "b", Buffer.from("b0"));
    const effect = makeEffect({ keys: ["a", "b", "c"] });
    effect.snapshot = await adapter.snapshot(effect);
    await adapter.apply(effect, async (client: FakeGcsClient) => {
      await put(client, "a", Buffer.from("a1"));
      await client.bucket(BUCKET).file("b").delete();
      await put(client, "c", Buffer.from("c1"));
    });
    await adapter.restore(effect.snapshot);
    expect((await get(gcs, "a"))?.toString()).toBe("a0");
    expect((await get(gcs, "b"))?.toString()).toBe("b0");
    expect(await get(gcs, "c")).toBeNull();
  });

  // --- partial failure ---

  it("restores captured keys even when apply raises mid-effect (partial failure)", async () => {
    const { gcs, adapter } = fresh();
    await put(gcs, "x", Buffer.from("x0"));
    await put(gcs, "y", Buffer.from("y0"));
    const effect = makeEffect({ keys: ["x", "y"] });
    effect.snapshot = await adapter.snapshot(effect);
    await expect(
      adapter.apply(effect, async (client: FakeGcsClient) => {
        await put(client, "x", Buffer.from("x1"));
        throw new Error("boom mid-effect");
      }),
    ).rejects.toThrow("boom");
    await adapter.restore(effect.snapshot);
    expect((await get(gcs, "x"))?.toString()).toBe("x0");
    expect((await get(gcs, "y"))?.toString()).toBe("y0");
  });

  it("payload is JSON-serialisable (base64 body keeps raw bytes JSON-light)", async () => {
    const { gcs, adapter } = fresh();
    await put(gcs, "p.bin", Buffer.from([0, 1, 2, 98, 121, 116, 101, 115]));
    const effect = makeEffect({ keys: ["p.bin", "absent.bin"] });
    const handle = await adapter.snapshot(effect);
    expect(() => JSON.stringify(handle.payload)).not.toThrow();
  });

  it("injects the client as the tool's first arg", async () => {
    const { gcs, adapter } = fresh();
    const effect = makeEffect({ key: "z.bin" });
    effect.snapshot = await adapter.snapshot(effect);
    let seen: { client: unknown; key: string } | undefined;
    await adapter.apply(effect, (client: FakeGcsClient, args: { key: string }) => {
      seen = { client, key: args.key };
    });
    expect(seen!.client).toBe(gcs);
    expect(seen!.key).toBe("z.bin");
  });

  it("snapshots nothing when the effect names no blob", async () => {
    const { adapter } = fresh();
    const effect = makeEffect({ unrelated: "value" });
    const handle = await adapter.snapshot(effect);
    expect(handle.payload["blobs"]).toEqual({});
    await expect(adapter.restore(handle)).resolves.toBeUndefined();
  });
});
