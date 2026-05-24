/**
 * MongoAdapter tests — mirror of tests/test_adapters_mongodb.py.
 *
 * Runs fully offline against an in-memory FakeMongoDatabase implementing the
 * findOne/replaceOne/deleteOne CRUD surface — a genuine document-snapshot ->
 * mutate -> restore round-trip, no replica set / no real server.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { Effect } from "../src/index.js";
import {
  MongoAdapter,
  type MongoCollection,
  type MongoDatabase,
  type MongoDoc,
} from "../src/adapters/index.js";

class FakeCollection implements MongoCollection {
  private docs = new Map<string, MongoDoc>();
  private idKey(id: unknown): string {
    return JSON.stringify(id);
  }
  findOne(filter: { _id: unknown }): MongoDoc | null {
    const d = this.docs.get(this.idKey(filter._id));
    return d ? structuredClone(d) : null;
  }
  replaceOne(filter: { _id: unknown }, replacement: MongoDoc, options?: { upsert?: boolean }): unknown {
    const k = this.idKey(filter._id);
    if (!this.docs.has(k) && !options?.upsert) return { matchedCount: 0 };
    this.docs.set(k, structuredClone(replacement));
    return { matchedCount: 1 };
  }
  deleteOne(filter: { _id: unknown }): unknown {
    this.docs.delete(this.idKey(filter._id));
    return { deletedCount: 1 };
  }
  // test/tool helpers
  insertOne(doc: MongoDoc): void {
    this.docs.set(this.idKey(doc._id), structuredClone(doc));
  }
  updateSet(id: unknown, patch: Record<string, unknown>): void {
    const k = this.idKey(id);
    const cur = this.docs.get(k);
    if (cur) this.docs.set(k, { ...cur, ...patch });
  }
  find(id: unknown): MongoDoc | null {
    return this.findOne({ _id: id });
  }
}

class FakeMongoDatabase implements MongoDatabase {
  private colls = new Map<string, FakeCollection>();
  collection(name: string): FakeCollection {
    let c = this.colls.get(name);
    if (!c) {
      c = new FakeCollection();
      this.colls.set(name, c);
    }
    return c;
  }
}

function effect(args: Record<string, unknown>, index = 0): Effect {
  return new Effect({ txnId: "t", index, tool: "fake", args, resource: "mongodb", reversible: true });
}

let db: FakeMongoDatabase;
let adapter: MongoAdapter;

beforeEach(() => {
  db = new FakeMongoDatabase();
  adapter = new MongoAdapter(db);
});

describe("MongoAdapter", () => {
  it("is honestly reversible and named", () => {
    expect(adapter.supportsRollback()).toBe(true);
    expect(adapter.name).toBe("mongodb");
  });

  // --- left-inverse ---------------------------------------------------------
  it("restores a modified document to its original", async () => {
    db.collection("users").insertOne({ _id: "u1", name: "alice", tier: "free" });
    const e = effect({ collection: "users", docId: "u1" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (database: FakeMongoDatabase, args: { collection: string; docId: unknown }) => {
      database.collection(args.collection).updateSet(args.docId, { tier: "premium" });
    });
    expect(db.collection("users").find("u1")!["tier"]).toBe("premium");
    await adapter.restore(e.snapshot);
    expect(db.collection("users").find("u1")).toEqual({ _id: "u1", name: "alice", tier: "free" });
  });

  it("deletes an inserted document on restore", async () => {
    const e = effect({ collection: "users", docId: "u2" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (database: FakeMongoDatabase, args: { collection: string; docId: unknown }) => {
      database.collection(args.collection).insertOne({ _id: args.docId, name: "bob" });
    });
    expect(db.collection("users").find("u2")).not.toBeNull();
    await adapter.restore(e.snapshot);
    expect(db.collection("users").find("u2")).toBeNull();
  });

  it("recreates a deleted pre-existing document on restore", async () => {
    db.collection("users").insertOne({ _id: "u3", name: "carol", score: 42 });
    const e = effect({ collection: "users", docId: "u3" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (database: FakeMongoDatabase, args: { collection: string; docId: unknown }) => {
      database.collection(args.collection).deleteOne({ _id: args.docId });
    });
    expect(db.collection("users").find("u3")).toBeNull();
    await adapter.restore(e.snapshot);
    expect(db.collection("users").find("u3")).toEqual({ _id: "u3", name: "carol", score: 42 });
  });

  it("deep-copies the snapshot so an in-place mutation does not corrupt it", async () => {
    db.collection("docs").insertOne({ _id: "d1", items: ["a", "b"] });
    const e = effect({ collection: "docs", docId: "d1" });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (database: FakeMongoDatabase, args: { collection: string; docId: unknown }) => {
      database.collection(args.collection).updateSet(args.docId, { items: ["x", "y", "z"] });
    });
    await adapter.restore(e.snapshot);
    expect(db.collection("docs").find("d1")!["items"]).toEqual(["a", "b"]);
  });

  it("restores all documents in a multi-document effect", async () => {
    db.collection("coll").insertOne({ _id: "a", v: 0 });
    db.collection("coll").insertOne({ _id: "b", v: 0 });
    const e = effect({
      docs: [
        { collection: "coll", docId: "a" },
        { collection: "coll", docId: "b" },
        { collection: "coll", docId: "c" },
      ],
    });
    e.snapshot = await adapter.snapshot(e);
    await adapter.apply(e, (database: FakeMongoDatabase) => {
      database.collection("coll").updateSet("a", { v: 1 });
      database.collection("coll").deleteOne({ _id: "b" });
      database.collection("coll").insertOne({ _id: "c", v: 1 });
    });
    await adapter.restore(e.snapshot);
    expect(db.collection("coll").find("a")).toEqual({ _id: "a", v: 0 });
    expect(db.collection("coll").find("b")).toEqual({ _id: "b", v: 0 });
    expect(db.collection("coll").find("c")).toBeNull();
  });

  // --- partial failure ------------------------------------------------------
  it("partial failure: tool mutates then throws, restore lands every captured doc", async () => {
    db.collection("coll").insertOne({ _id: "x", v: 0 });
    db.collection("coll").insertOne({ _id: "y", v: 0 });
    const e = effect({
      docs: [
        { collection: "coll", docId: "x" },
        { collection: "coll", docId: "y" },
      ],
    });
    e.snapshot = await adapter.snapshot(e);
    expect(() =>
      adapter.apply(e, (database: FakeMongoDatabase) => {
        database.collection("coll").updateSet("x", { v: 99 });
        throw new Error("boom mid-effect");
      }),
    ).toThrow("boom");
    await adapter.restore(e.snapshot);
    expect(db.collection("coll").find("x")).toEqual({ _id: "x", v: 0 });
    expect(db.collection("coll").find("y")).toEqual({ _id: "y", v: 0 });
  });

  it("payload is JSON-serialisable", async () => {
    db.collection("users").insertOne({ _id: "p", name: "x", n: 5 });
    const e = effect({
      docs: [
        { collection: "users", docId: "p" },
        { collection: "users", docId: "absent" },
      ],
    });
    e.snapshot = await adapter.snapshot(e);
    expect(() => JSON.stringify(e.snapshot!.payload)).not.toThrow();
  });

  it("injects the db as the first arg", async () => {
    const e = effect({ collection: "users", docId: "z" });
    e.snapshot = await adapter.snapshot(e);
    const seen: Record<string, unknown> = {};
    await adapter.apply(e, (database: FakeMongoDatabase, args: { collection: string; docId: unknown }) => {
      seen["db"] = database;
      seen["collection"] = args.collection;
      seen["docId"] = args.docId;
    });
    expect(seen["db"]).toBe(db);
    expect(seen["collection"]).toBe("users");
    expect(seen["docId"]).toBe("z");
  });

  it("captures nothing when the effect touches no document", async () => {
    const e = effect({ unrelated: "value" });
    e.snapshot = await adapter.snapshot(e);
    expect(e.snapshot.payload["docs"]).toEqual([]);
    await expect(adapter.restore(e.snapshot)).resolves.toBeUndefined();
  });
});
