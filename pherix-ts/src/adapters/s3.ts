/**
 * S3Adapter — snapshot-the-touched-object reversibility over an object store.
 * Mirror of pherix/core/adapters/s3.py.
 *
 * S3 has no native savepoint or transaction model: a `putObject` /
 * `deleteObject` lands immediately and irreversibly at the storage layer. Yet
 * the *single object* an effect touches is small enough to copy. So
 * reversibility here is the same machinery as the FilesystemAdapter — capture
 * the **before-image** of the touched object(s) in `snapshot`, and write that
 * image back in `restore`. The object store does no undo for us (unlike SQLite
 * savepoints); we do it by hand, honestly, against real bytes.
 *
 * Touched-keys convention (route b). The adapter learns which object(s) an
 * effect touches by reading them off `effect.args` by name:
 *   - A single object: `args.key` is the object key (string).
 *   - Multiple objects in one effect: `args.keys` is a list of object keys.
 * If neither is present, the effect touches no object and `snapshot` captures
 * nothing — restore is then a no-op. The bucket is fixed at adapter
 * construction (one adapter == one bucket). A before-image records, per key,
 * either the prior bytes (object existed) or an *absent* marker.
 *
 * Atomicity (honest). S3 gives no cross-object atomicity, and neither does this
 * adapter: a multi-key effect that fails partway leaves some objects mutated.
 * That is what the journal backward-fold is for — `restore` rewrites every
 * captured key back to its before-image regardless of how far `apply` got.
 *
 * The driver is kept structural (`S3Client`) so this module forces no AWS SDK
 * types on consumers and tests can substitute an in-memory fake. The client is
 * constructed by the caller (their dependency, lazily), mirroring how the
 * Python adapter never imports boto3 itself.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

/** A boto3-shaped error: `name`/`Code` distinguish a missing key from a real
 *  failure. We treat NoSuchKey / 404 (and a NotFound name) as "absent". */
function isMissingKeyError(exc: unknown): boolean {
  const e = exc as { name?: string; Code?: string; code?: string } | undefined;
  const code = e?.Code ?? e?.code ?? e?.name ?? "";
  return code === "NoSuchKey" || code === "404" || code === "NotFound";
}

/** The slice of an S3 client surface this adapter uses. Methods return the
 *  AWS-SDK-v3 shape (`{ Body }` where Body yields bytes). A fake satisfies the
 *  same async contract offline. */
export interface S3Client {
  getObject(input: { Bucket: string; Key: string }): Promise<{ Body: Uint8Array | { transformToByteArray(): Promise<Uint8Array> } }>;
  putObject(input: { Bucket: string; Key: string; Body: Uint8Array }): Promise<unknown>;
  deleteObject(input: { Bucket: string; Key: string }): Promise<unknown>;
}

interface ObjectRecord {
  existed: boolean;
  body: string | null; // base64 of prior bytes, or null when absent
}

/** Normalise an AWS-SDK-v3 / fake Body into raw bytes. */
async function readBody(body: { transformToByteArray?: () => Promise<Uint8Array> } | Uint8Array): Promise<Uint8Array> {
  if (body instanceof Uint8Array) return body;
  if (typeof body.transformToByteArray === "function") return body.transformToByteArray();
  throw new TypeError("S3 getObject returned an unrecognised Body shape");
}

export class S3Adapter implements ResourceAdapter {
  readonly name = "s3";
  private readonly s3: S3Client;
  private readonly bucketName: string;

  constructor(client: S3Client, bucket: string) {
    this.s3 = client;
    this.bucketName = bucket;
  }

  get client(): S3Client {
    return this.s3;
  }

  get bucket(): string {
    return this.bucketName;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- touched-key extraction --------------------------------------------

  private static touchedKeys(effect: Effect): string[] {
    const keys: string[] = [];
    const single = effect.args["key"];
    if (single !== undefined && single !== null) keys.push(single as string);
    const multi = effect.args["keys"];
    if (multi) keys.push(...(multi as string[]));
    // De-dup while preserving first-seen order.
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

  // --- per-effect snapshot / apply / restore -----------------------------

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const records: Record<string, ObjectRecord> = {};
    for (const key of S3Adapter.touchedKeys(effect)) {
      try {
        const resp = await this.s3.getObject({ Bucket: this.bucketName, Key: key });
        const body = await readBody(resp.Body);
        records[key] = {
          existed: true,
          body: Buffer.from(body).toString("base64"),
        };
      } catch (exc) {
        // NoSuchKey / 404 means "absent" — record it so restore deletes
        // whatever the effect creates. Any other error (permissions, network)
        // is a real failure and must surface.
        if (isMissingKeyError(exc)) {
          records[key] = { existed: false, body: null };
        } else {
          throw exc;
        }
      }
    }
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { bucket: this.bucketName, objects: records },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The S3 client is injected as the tool's first positional arg, exactly as
    // SqliteAdapter injects the connection. The @tool wrapper hides it from the
    // agent's call-site.
    return toolFn(this.s3, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const bucket = handle.payload["bucket"] as string;
    const objects = handle.payload["objects"] as Record<string, ObjectRecord>;
    for (const [key, record] of Object.entries(objects)) {
      if (record.existed) {
        const body = new Uint8Array(Buffer.from(record.body as string, "base64"));
        await this.s3.putObject({ Bucket: bucket, Key: key, Body: body });
      } else {
        // deleteObject is a no-op on a missing key in S3, so this is safe
        // whether the effect created the object or not.
        await this.s3.deleteObject({ Bucket: bucket, Key: key });
      }
    }
  }
}
