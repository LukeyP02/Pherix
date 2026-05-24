/**
 * ElasticsearchAdapter — snapshot-the-touched-document reversibility, one index.
 * Mirror of pherix/core/adapters/elasticsearch.py.
 *
 * Elasticsearch has no document-level savepoint: an index / delete lands
 * immediately (and becomes searchable after a refresh). But a document's
 * `_source` is copyable, so reversibility is the Mongo-adapter machinery
 * applied to ES — capture the before-image of every touched document in
 * `snapshot()`, rewrite it in `restore()` (re-index the prior source if it
 * existed, delete it if it did not).
 *
 * One adapter speaks for one index, mirroring "one S3Adapter == one bucket".
 * The touched-key convention is route-b: document id(s) come from `args.key`
 * (single) and/or `args.keys` (list).
 *
 * Absence is decided with `client.exists(...)` rather than catching a
 * NotFoundError — keeping the kernel free of compile-time knowledge of
 * `@elastic/elasticsearch` (the dependency is the caller's, pulled lazily).
 * Restore writes pass `refresh: true` so the undo is immediately visible to a
 * subsequent read within the same fold; ES is near-real-time, and an
 * unrefreshed restore could otherwise be missed by the next snapshot/read.
 *
 * `@elastic/elasticsearch` is never imported by this module; the caller
 * constructs the client. The same adapter speaks to OpenSearch via the
 * API-compatible client.
 *
 * Honesty caveat: reversible (the backward fold restores the document), but no
 * version contract — so ES effects do not participate in commit-time isolation
 * diffing, exactly as S3/Redis/Mongo.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

/** The slice of an Elasticsearch client this adapter uses. Mirrors
 *  `@elastic/elasticsearch`'s Client (and the API-compatible OpenSearch
 *  client): exists / get / index / delete, all promise-returning. Kept
 *  structural so no SDK type is forced. */
export interface EsClient {
  exists(params: { index: string; id: string }): Promise<boolean>;
  get(params: { index: string; id: string }): Promise<{ _source: Record<string, unknown> }>;
  index(params: { index: string; id: string; document: Record<string, unknown>; refresh?: boolean }): Promise<unknown>;
  delete(params: { index: string; id: string; refresh?: boolean }): Promise<unknown>;
}

interface DocRecord {
  existed: boolean;
  doc: Record<string, unknown> | null;
}

export class ElasticsearchAdapter implements ResourceAdapter {
  readonly name = "elasticsearch";
  private readonly client: EsClient;
  private readonly indexName: string;

  constructor(client: EsClient, index: string) {
    this.client = client;
    this.indexName = index;
  }

  get connection(): EsClient {
    return this.client;
  }

  get index(): string {
    return this.indexName;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- touched-key extraction ---

  /** Document ids this effect touches, per the route-b convention. `args.key`
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
    const records: Record<string, DocRecord> = {};
    for (const docId of ElasticsearchAdapter.touchedKeys(effect)) {
      if (await this.client.exists({ index: this.indexName, id: docId })) {
        const resp = await this.client.get({ index: this.indexName, id: docId });
        // Deep-copy the source so a later in-place mutation of the live doc
        // cannot change the captured before-image. Sources are JSON-light, the
        // same contract the Mongo adapter carries.
        records[docId] = { existed: true, doc: structuredClone(resp._source) };
      } else {
        records[docId] = { existed: false, doc: null };
      }
    }
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { index: this.indexName, docs: records },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The ES client is injected as the tool's first arg, exactly as
    // SqliteAdapter injects the connection. The tool wrapper hides it.
    return toolFn(this.client, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const index = handle.payload["index"] as string;
    const docs = handle.payload["docs"] as Record<string, DocRecord>;
    for (const [docId, record] of Object.entries(docs)) {
      if (record.existed) {
        await this.client.index({
          index,
          id: docId,
          document: record.doc as Record<string, unknown>,
          refresh: true,
        });
      } else if (await this.client.exists({ index, id: docId })) {
        await this.client.delete({ index, id: docId, refresh: true });
      }
    }
  }
}
