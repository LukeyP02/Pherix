/**
 * MongoAdapter — snapshot-the-touched-document reversibility over a document store.
 * Mirror of pherix/core/adapters/mongodb.py.
 *
 * Standalone MongoDB has no multi-document transaction (those need a replica
 * set). But the *document* an effect touches, addressed by `_id`, is small
 * enough to copy. So reversibility is the filesystem-adapter machinery once
 * more — capture the **before-image** of each touched document in `snapshot`,
 * write it back in `restore`. This works on *any* MongoDB deployment because it
 * only uses ordinary CRUD (`findOne` / `replaceOne` / `deleteOne`).
 *
 * Touched-document convention (route b). The adapter learns which document(s)
 * an effect touches from `effect.args`:
 *   - A single document: `args.collection` (name) + `args.docId` (its `_id`).
 *   - Multiple documents: `args.docs` — a list of `{collection, docId}`.
 * If no document is named, nothing is captured and restore is a no-op. A
 * before-image records, per (collection, _id), the prior document (existed) or
 * an *absent* marker.
 *
 * Atomicity (honest). On standalone Mongo a multi-document effect is not atomic
 * at the server; the journal backward-fold is what makes the *effect* atomic —
 * `restore` rewrites every captured document back regardless of how far `apply`
 * got. A single-document `replaceOne` / `deleteOne` is atomic at the document
 * level in MongoDB.
 *
 * The driver is kept structural (`MongoDatabase` / `MongoCollection`) so this
 * module forces no `mongodb` driver types on consumers and tests can substitute
 * an in-memory fake. The before-image is held as plain values, deep-copied so
 * the effect cannot mutate it in place.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

export type MongoDoc = Record<string, unknown> & { _id: unknown };

/** The slice of a pymongo/mongodb Collection surface this adapter uses. */
export interface MongoCollection {
  findOne(filter: { _id: unknown }): Promise<MongoDoc | null> | MongoDoc | null;
  replaceOne(
    filter: { _id: unknown },
    replacement: MongoDoc,
    options?: { upsert?: boolean },
  ): Promise<unknown> | unknown;
  deleteOne(filter: { _id: unknown }): Promise<unknown> | unknown;
}

/** A Mongo Database: collections addressed by name, as `db[name]` in Python /
 *  `db.collection(name)` in the node driver. We use a single accessor so a fake
 *  and the real driver both satisfy it. */
export interface MongoDatabase {
  collection(name: string): MongoCollection;
}

interface DocTarget {
  collection: string;
  docId: unknown;
}

interface DocRecord {
  collection: string;
  docId: unknown;
  existed: boolean;
  doc: MongoDoc | null;
}

/** Independent deep copy of a JSON-light document, so the effect mutating the
 *  returned object in place cannot corrupt the snapshot. Mirrors Python's
 *  copy.deepcopy on a document of JSON-light values. */
function deepCopy<T>(value: T): T {
  return structuredClone(value);
}

export class MongoAdapter implements ResourceAdapter {
  readonly name = "mongodb";
  private readonly database: MongoDatabase;

  constructor(db: MongoDatabase) {
    this.database = db;
  }

  get db(): MongoDatabase {
    return this.database;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- touched-document extraction ---------------------------------------

  private static touchedDocs(effect: Effect): DocTarget[] {
    const targets: DocTarget[] = [];
    const coll = effect.args["collection"];
    const docId = effect.args["docId"];
    if (coll !== undefined && coll !== null && docId !== undefined && docId !== null) {
      targets.push({ collection: coll as string, docId });
    }
    for (const entry of (effect.args["docs"] as DocTarget[] | undefined) ?? []) {
      targets.push({ collection: entry.collection, docId: entry.docId });
    }
    const seen = new Set<string>();
    const out: DocTarget[] = [];
    for (const t of targets) {
      const sig = JSON.stringify([t.collection, t.docId]);
      if (!seen.has(sig)) {
        seen.add(sig);
        out.push(t);
      }
    }
    return out;
  }

  // --- per-effect snapshot / apply / restore -----------------------------

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const records: DocRecord[] = [];
    for (const target of MongoAdapter.touchedDocs(effect)) {
      const existing = await this.database
        .collection(target.collection)
        .findOne({ _id: target.docId });
      records.push({
        collection: target.collection,
        docId: target.docId,
        existed: existing !== null && existing !== undefined,
        doc: existing !== null && existing !== undefined ? deepCopy(existing) : null,
      });
    }
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { docs: records },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The Mongo database is injected as the tool's first positional arg, as
    // SqliteAdapter injects the connection. The @tool wrapper hides it.
    return toolFn(this.database, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const records = handle.payload["docs"] as DocRecord[];
    for (const record of records) {
      const coll = this.database.collection(record.collection);
      if (record.existed) {
        // upsert=true so a document the effect deleted is re-created, and one
        // the effect merely modified is overwritten back.
        await coll.replaceOne({ _id: record.docId }, record.doc as MongoDoc, {
          upsert: true,
        });
      } else {
        await coll.deleteOne({ _id: record.docId });
      }
    }
  }
}
