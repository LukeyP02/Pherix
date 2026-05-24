/**
 * DynamoDbAdapter — snapshot-the-touched-item reversibility over one table.
 * Mirror of pherix/core/adapters/dynamodb.py.
 *
 * DynamoDB has no per-write savepoint: a PutItem / DeleteItem lands
 * immediately. But a single item is small enough to copy, so reversibility is
 * the S3/Redis machinery applied to items — capture the before-image of every
 * touched item in `snapshot()` (GetItem), rewrite it in `restore()` (PutItem if
 * it existed, DeleteItem if it did not).
 *
 * One adapter speaks for one table, addressed by a single string partition key
 * (`keyAttr`, default "pk") — mirroring "one S3Adapter == one bucket". The
 * touched-key convention is route-b, identical to S3/Redis: the partition-key
 * value(s) come from `args.key` (single) and/or `args.keys` (list). An effect
 * that names neither touches nothing.
 *
 * The AWS SDK client is injected by the caller (it constructs it); this module
 * never imports `@aws-sdk/*`, so `import` of the SDK stays the caller's, lazily
 * — the kernel is dependency-free. The before-image is the raw low-level item
 * dict (typed-attribute form, e.g. {"pk":{"S":"a"},"v":{"S":"1"}}) — already
 * JSON-light, so the audit journal serialises it with no extra encoding.
 *
 * Honesty caveat: like the other store adapters this is reversible (the
 * backward fold restores the item) but it does NOT implement the version
 * contract, so its effects do not participate in commit-time isolation diffing
 * — exactly as S3/Redis/Mongo.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

/** The slice of a DynamoDB client this adapter uses. A real `@aws-sdk/client-dynamodb`
 *  DynamoDB / DynamoDBClient (low-level, `.getItem/.putItem/.deleteItem`) or a
 *  promise-returning fake both satisfy it. Kept structural so no SDK type is forced. */
export interface DynamoDbClient {
  getItem(params: { TableName: string; Key: Record<string, unknown> }): Promise<{ Item?: Record<string, unknown> }>;
  putItem(params: { TableName: string; Item: Record<string, unknown> }): Promise<unknown>;
  deleteItem(params: { TableName: string; Key: Record<string, unknown> }): Promise<unknown>;
}

interface ItemRecord {
  existed: boolean;
  item: Record<string, unknown> | null;
}

export class DynamoDbAdapter implements ResourceAdapter {
  readonly name = "dynamodb";
  private readonly client: DynamoDbClient;
  private readonly table: string;
  private readonly keyAttr: string;

  constructor(client: DynamoDbClient, table: string, options: { keyAttr?: string } = {}) {
    this.client = client;
    this.table = table;
    this.keyAttr = options.keyAttr ?? "pk";
  }

  get connection(): DynamoDbClient {
    return this.client;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- touched-key extraction ---

  /** Partition-key values this effect touches, per the route-b convention.
   *  `args.key` (single) and/or `args.keys` (list); union, de-duplicated,
   *  order-preserving. */
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

  private keyDict(value: string): Record<string, unknown> {
    // Partition keys are addressed as strings ({"S": value}). A table whose PK
    // is numeric/binary is the caller's to adapt; the base contract is the
    // common string-keyed table.
    return { [this.keyAttr]: { S: value } };
  }

  // --- per-effect snapshot / apply / restore ---

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const records: Record<string, ItemRecord> = {};
    for (const key of DynamoDbAdapter.touchedKeys(effect)) {
      const resp = await this.client.getItem({ TableName: this.table, Key: this.keyDict(key) });
      const item = resp.Item;
      records[key] =
        item !== undefined && item !== null
          ? { existed: true, item }
          : { existed: false, item: null };
    }
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { table: this.table, items: records },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The DynamoDB client is injected as the tool's first arg, exactly as
    // SqliteAdapter injects the connection. The tool wrapper hides the handle
    // from the agent's call-site, then passes the named-args object.
    return toolFn(this.client, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const table = handle.payload["table"] as string;
    const items = handle.payload["items"] as Record<string, ItemRecord>;
    for (const [key, record] of Object.entries(items)) {
      if (record.existed) {
        await this.client.putItem({ TableName: table, Item: record.item as Record<string, unknown> });
      } else {
        await this.client.deleteItem({ TableName: table, Key: this.keyDict(key) });
      }
    }
  }
}
