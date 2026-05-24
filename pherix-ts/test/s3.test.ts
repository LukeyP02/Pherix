/**
 * S3Adapter tests — mirror of tests/test_adapters_s3.py.
 *
 * Runs fully offline against an in-memory FakeS3Client implementing the async
 * getObject/putObject/deleteObject surface — a genuine snapshot -> mutate ->
 * restore round-trip against bytes, not a stub.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { Effect } from "../src/index.js";
import { S3Adapter, type S3Client } from "../src/adapters/index.js";

const BUCKET = "pherix-test-bucket";

/** A boto3-shaped missing-key error. */
class NoSuchKeyError extends Error {
  readonly name = "NoSuchKey";
  readonly Code = "NoSuchKey";
}

/** In-memory S3: a map keyed by `${bucket}/${key}` of raw bytes. getObject on a
 *  missing key throws a NoSuchKey error, exactly as real S3 / moto does. */
class FakeS3Client implements S3Client {
  store = new Map<string, Uint8Array>();

  async getObject(input: { Bucket: string; Key: string }): Promise<{ Body: Uint8Array }> {
    const v = this.store.get(`${input.Bucket}/${input.Key}`);
    if (v === undefined) throw new NoSuchKeyError(`no such key ${input.Key}`);
    return { Body: v };
  }

  async putObject(input: { Bucket: string; Key: string; Body: Uint8Array }): Promise<unknown> {
    this.store.set(`${input.Bucket}/${input.Key}`, input.Body);
    return {};
  }

  async deleteObject(input: { Bucket: string; Key: string }): Promise<unknown> {
    this.store.delete(`${input.Bucket}/${input.Key}`);
    return {};
  }

  // test helpers
  put(key: string, body: string): void {
    this.store.set(`${BUCKET}/${key}`, new TextEncoder().encode(body));
  }
  has(key: string): boolean {
    return this.store.has(`${BUCKET}/${key}`);
  }
  text(key: string): string {
    return new TextDecoder().decode(this.store.get(`${BUCKET}/${key}`)!);
  }
}

function effect(args: Record<string, unknown>, index = 0): Effect {
  return new Effect({ txnId: "t", index, tool: "fake", args, resource: "s3", reversible: true });
}

let s3: FakeS3Client;
let adapter: S3Adapter;

beforeEach(() => {
  s3 = new FakeS3Client();
  adapter = new S3Adapter(s3, BUCKET);
});

describe("S3Adapter", () => {
  it("is honestly reversible and named", () => {
    expect(adapter.supportsRollback()).toBe(true);
    expect(adapter.name).toBe("s3");
  });

  // --- left-inverse: restore ∘ apply ≈ identity -----------------------------
  it("restores a modified object to its original bytes", async () => {
    s3.put("doc.bin", "original");
    const e = effect({ key: "doc.bin" });
    e.snapshot = await adapter.snapshot(e);

    await adapter.apply(e, async (client: S3Client, args: { key: string }) => {
      await client.putObject({ Bucket: BUCKET, Key: args.key, Body: new TextEncoder().encode("modified") });
    });
    expect(s3.text("doc.bin")).toBe("modified");

    await adapter.restore(e.snapshot);
    expect(s3.text("doc.bin")).toBe("original");
  });

  it("deletes a created object on restore", async () => {
    const e = effect({ key: "new.bin" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, async (client: S3Client, args: { key: string }) => {
      await client.putObject({ Bucket: BUCKET, Key: args.key, Body: new TextEncoder().encode("hello") });
    });
    expect(s3.has("new.bin")).toBe(true);

    await adapter.restore(e.snapshot);
    expect(s3.has("new.bin")).toBe(false);
  });

  it("recreates a deleted pre-existing object on restore", async () => {
    s3.put("keep.bin", "precious");
    const e = effect({ key: "keep.bin" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, async (client: S3Client, args: { key: string }) => {
      await client.deleteObject({ Bucket: BUCKET, Key: args.key });
    });
    expect(s3.has("keep.bin")).toBe(false);

    await adapter.restore(e.snapshot);
    expect(s3.text("keep.bin")).toBe("precious");
  });

  it("restores all objects in a multi-key effect", async () => {
    s3.put("a", "a0");
    s3.put("b", "b0");
    const e = effect({ keys: ["a", "b", "c"] });
    e.snapshot = await adapter.snapshot(e);

    await adapter.apply(e, async (client: S3Client) => {
      await client.putObject({ Bucket: BUCKET, Key: "a", Body: new TextEncoder().encode("a1") });
      await client.deleteObject({ Bucket: BUCKET, Key: "b" });
      await client.putObject({ Bucket: BUCKET, Key: "c", Body: new TextEncoder().encode("c1") });
    });
    await adapter.restore(e.snapshot);

    expect(s3.text("a")).toBe("a0");
    expect(s3.text("b")).toBe("b0");
    expect(s3.has("c")).toBe(false);
  });

  // --- partial failure ------------------------------------------------------
  it("partial failure: tool mutates then throws, restore lands every captured key", async () => {
    s3.put("x", "x0");
    s3.put("y", "y0");
    const e = effect({ keys: ["x", "y"] });
    e.snapshot = await adapter.snapshot(e);

    await expect(
      adapter.apply(e, async (client: S3Client) => {
        await client.putObject({ Bucket: BUCKET, Key: "x", Body: new TextEncoder().encode("x1") });
        throw new Error("boom mid-effect");
      }),
    ).rejects.toThrow("boom");

    await adapter.restore(e.snapshot);
    expect(s3.text("x")).toBe("x0");
    expect(s3.text("y")).toBe("y0");
  });

  it("payload is JSON-serialisable (base64-encoded bodies)", async () => {
    s3.store.set(`${BUCKET}/p.bin`, new Uint8Array([0, 1, 2, 98, 121, 116, 101, 115]));
    const e = effect({ keys: ["p.bin", "absent.bin"] });
    e.snapshot = await adapter.snapshot(e);
    expect(() => JSON.stringify(e.snapshot!.payload)).not.toThrow();
  });

  it("injects the client as the first arg", async () => {
    const e = effect({ key: "z.bin" });
    e.snapshot = await adapter.snapshot(e);
    const seen: Record<string, unknown> = {};
    await adapter.apply(e, (client: S3Client, args: { key: string }) => {
      seen["client"] = client;
      seen["key"] = args.key;
    });
    expect(seen["client"]).toBe(s3);
    expect(seen["key"]).toBe("z.bin");
  });

  it("propagates a non-missing client error from snapshot", async () => {
    const throwing: S3Client = {
      async getObject() {
        const err = new Error("AccessDenied") as Error & { Code: string };
        err.Code = "AccessDenied";
        throw err;
      },
      async putObject() {
        return {};
      },
      async deleteObject() {
        return {};
      },
    };
    const bad = new S3Adapter(throwing, "no-bucket");
    await expect(bad.snapshot(effect({ key: "whatever" }))).rejects.toThrow("AccessDenied");
  });

  it("captures nothing when the effect touches no object", async () => {
    const e = effect({ unrelated: "value" });
    e.snapshot = await adapter.snapshot(e);
    expect(e.snapshot.payload["objects"]).toEqual({});
    await expect(adapter.restore(e.snapshot)).resolves.toBeUndefined();
  });
});
