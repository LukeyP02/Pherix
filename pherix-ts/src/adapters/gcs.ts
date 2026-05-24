/**
 * GcsAdapter — snapshot-the-touched-blob reversibility over one GCS bucket.
 * Mirror of pherix/core/adapters/gcs.py.
 *
 * Google Cloud Storage has no object-level savepoint: an upload / delete lands
 * immediately. But a blob's bytes are copyable, so reversibility is the
 * S3-adapter machinery applied to GCS — capture the before-image of every
 * touched blob in `snapshot()`, rewrite it in `restore()` (re-upload the prior
 * bytes if it existed, delete it if it did not).
 *
 * One adapter speaks for one bucket, mirroring "one S3Adapter == one bucket".
 * The touched-key convention is route-b, identical to S3: blob name(s) come
 * from `args.key` (single) and/or `args.keys` (list).
 *
 * Absence is decided with `blob.exists()` rather than catching the client's
 * NotFound — this keeps the kernel free of any compile-time knowledge of
 * `@google-cloud/storage`'s exception types (the dependency is the caller's,
 * pulled lazily). The cost is one extra metadata round-trip per touched blob,
 * negligible against the upload it guards.
 *
 * `@google-cloud/storage` is never imported by this module; the caller
 * constructs the client, so importing it stays dependency-free.
 *
 * Honesty caveat: reversible (the backward fold restores the blob), but no
 * version contract — so GCS effects do not participate in commit-time isolation
 * diffing, exactly as S3/Redis/Mongo.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

/** The slice of a GCS blob this adapter uses. Mirrors `@google-cloud/storage`'s
 *  File: exists / download / save / delete (all promise-returning). */
export interface GcsBlob {
  exists(): Promise<[boolean]>;
  download(): Promise<[Buffer]>;
  save(data: Buffer | Uint8Array): Promise<unknown>;
  delete(): Promise<unknown>;
}

/** The slice of a GCS bucket this adapter uses. */
export interface GcsBucket {
  file(name: string): GcsBlob;
}

/** The slice of a GCS client this adapter uses: `bucket(name)`. A real
 *  `Storage` instance or a fake both satisfy it; kept structural so no SDK
 *  type is forced. */
export interface GcsClient {
  bucket(name: string): GcsBucket;
}

interface BlobRecord {
  existed: boolean;
  /** base64-encoded prior bytes (JSON-light for the audit journal), or null. */
  body: string | null;
}

export class GcsAdapter implements ResourceAdapter {
  readonly name = "gcs";
  private readonly client: GcsClient;
  private readonly bucketName: string;

  constructor(client: GcsClient, bucket: string) {
    this.client = client;
    this.bucketName = bucket;
  }

  get connection(): GcsClient {
    return this.client;
  }

  get bucket(): string {
    return this.bucketName;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- touched-key extraction ---

  /** Blob names this effect touches, per the route-b convention. `args.key`
   *  (single) and/or `args.keys` (list); union, de-duplicated, order-preserving. */
  private static touchedKeys(effect: Effect): string[] {
    const keys: string[] = [];
    const single = effect.args["key"];
    if (single !== undefined && single !== null) keys.push(single as string);
    const multi = effect.args["keys"];
    if (Array.isArray(multi)) keys.push(...(multi as string[]));
    const seen = new Set<string>();
    const out: string[] = [];
    for (const k of keys) {
      if (!seen.has(k)) {
        seen.add(k);
        out.push(k);
      }
    }
    return out;
  }

  // --- per-effect snapshot / apply / restore ---

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const bucket = this.client.bucket(this.bucketName);
    const records: Record<string, BlobRecord> = {};
    for (const key of GcsAdapter.touchedKeys(effect)) {
      const blob = bucket.file(key);
      const [present] = await blob.exists();
      if (present) {
        const [body] = await blob.download();
        records[key] = { existed: true, body: Buffer.from(body).toString("base64") };
      } else {
        records[key] = { existed: false, body: null };
      }
    }
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { bucket: this.bucketName, blobs: records },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The GCS client is injected as the tool's first arg, exactly as
    // SqliteAdapter injects the connection. The tool wrapper hides it.
    return toolFn(this.client, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const bucket = this.client.bucket(handle.payload["bucket"] as string);
    const blobs = handle.payload["blobs"] as Record<string, BlobRecord>;
    for (const [key, record] of Object.entries(blobs)) {
      const blob = bucket.file(key);
      if (record.existed) {
        await blob.save(Buffer.from(record.body as string, "base64"));
      } else {
        const [present] = await blob.exists();
        if (present) await blob.delete();
      }
    }
  }
}
