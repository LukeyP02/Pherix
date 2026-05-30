/*
 * SDK parity runner — the TypeScript half of tests/test_sdk_parity.py.
 *
 * Usage:  npx tsx test/parity/runner.mts <scenario-name>
 *
 * Runs ONE named scenario through the real `pherix-ts` SDK (agentTxn + tool +
 * the sql/fs/http adapters that already exist) and prints the resulting journal
 * to stdout as a single canonical-JSON line. The Python test runs the *same*
 * scenario in-process through `pherix`, builds the same canonical shape, and
 * asserts structural equality.
 *
 * The canonical shape (mirror of `_canonical_journal` in the Python test):
 *   {
 *     "final_state": "<TxnState string value>",   // e.g. "committed"
 *     "outcome":     "<ok | GateBlocked | IsolationConflict>",
 *     "effects": [
 *       {
 *         "index":      <int>,                     // journal position
 *         "tool":       "<tool name>",
 *         "resource":   "<adapter name>",
 *         "reversible": <bool>,
 *         "status":     "<EffectStatus string value>",  // e.g. "applied"
 *         "read_keys":  [[resource, key, version], ...],
 *         "write_keys": [[resource, key, versionAfter], ...]
 *       }, ...
 *     ]
 *   }
 *
 * Deliberately NORMALIZED OUT (these legitimately differ by language/run and are
 * NOT part of the structural contract — see the Python test's docstring for the
 * matching rationale):
 *   - txnId / txn_id            (random per run)
 *   - effectId / effect_id      (a content hash; the inputs that feed it — tool,
 *                                index, args — ARE asserted, so the hash itself
 *                                is redundant to compare and only risks a
 *                                cross-language hashing-detail false negative)
 *   - ts / timestamps           (wall clock)
 *   - result / snapshot payload (driver-specific objects; the STATUS transition
 *                                APPLIED/STAGED/GATED already captures the lane
 *                                outcome, which is the structural fact)
 *   - args                      (asserted indirectly via tool+order; values like
 *                                amounts are scenario-internal, not a parity claim)
 *
 * The enum string VALUES are identical across both SDKs by construction
 * (TxnState/EffectStatus share the same wire strings), so they are asserted
 * directly with no mapping.
 */

import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import Database from "better-sqlite3";
import {
  DynamoDbAdapter,
  Effect,
  ElasticsearchAdapter,
  FilesystemAdapter,
  FsHandle,
  GateBlocked,
  GcsAdapter,
  GitAdapter,
  GitHandle,
  HttpAdapter,
  IsolationConflict,
  MemoryAdapter,
  MemoryHandle,
  MongoAdapter,
  MqAdapter,
  MySQLAdapter,
  PostgresAdapter,
  type PgClient,
  type PgResult,
  REGISTRY,
  RedisAdapter,
  RestAdapter,
  S3Adapter,
  SqliteAdapter,
  type SqliteDatabase,
  TxnState,
  agentTxn,
  dryRun,
  executeIsolated,
  publishTool,
  restTool,
  tool,
} from "../../src/index.js";

interface CanonEffect {
  index: number;
  tool: string;
  resource: string;
  reversible: boolean;
  status: string;
  read_keys: unknown[];
  write_keys: unknown[];
}
interface CanonJournal {
  final_state: string;
  outcome: string;
  effects: CanonEffect[];
}

/** Build the canonical journal shape from a finished TxnContext + outcome tag. */
function canonical(txn: { state: TxnState; effects: Effect[] }, outcome: string): CanonJournal {
  return {
    final_state: txn.state,
    outcome,
    effects: txn.effects.map((e) => ({
      index: e.index,
      tool: e.tool,
      resource: e.resource,
      reversible: e.reversible,
      status: e.status,
      // Keys are tuple-shaped arrays already; JSON round-trips them identically
      // to Python's list-of-lists, so no further normalization is needed.
      read_keys: e.readKeys,
      write_keys: e.writeKeys,
    })),
  };
}

// --- scenarios -------------------------------------------------------------
// Each returns the canonical journal. The set is intentionally a registry
// (the `SCENARIOS` map below) so adding one per newly-ported adapter is a
// one-liner on each side.

/** reversible: a SQLite write that commits. Journal shows STAGED -> APPLIED. */
async function reversibleCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const db = new Database(":memory:");
  db.exec("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)");
  db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("alice", 100);
  db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("bob", 0);
  const sql = new SqliteAdapter(db);

  const transfer = tool<{ from: string; to: string; amount: number }>(
    "sql",
    (conn: SqliteDatabase, args) => {
      conn.prepare("UPDATE accounts SET balance = balance - ? WHERE name = ?").run(args.amount, args.from);
      conn.prepare("UPDATE accounts SET balance = balance + ? WHERE name = ?").run(args.amount, args.to);
      return { ok: true };
    },
    { name: "transfer" },
  );

  const ctx = await agentTxn({ sql }, async () => {
    await transfer({ from: "alice", to: "bob", amount: 30 });
  });
  const out = canonical(ctx.txn, "ok");
  db.close();
  return out;
}

/** irreversible + gate: an HTTP-style effect with no compensator. commit()
 *  blocks at the gate; the txn unwinds to ROLLED_BACK and the effect is GATED. */
async function irreversibleGate(): Promise<CanonJournal> {
  REGISTRY.clear();
  const http = new HttpAdapter();
  const fired: string[] = [];

  const sendEmail = tool<{ to: string; body: string }>(
    "http",
    (args) => {
      fired.push(JSON.stringify(args));
      return { delivered: true };
    },
    { name: "sendEmail", injectsHandle: false }, // no compensator -> gates
  );

  // agentTxn throws GateBlocked from commit *after* mutating the ctx in place,
  // and the throw discards the returned ctx — so capture the live ctx inside
  // the body (same pattern as isolationConflict below).
  let outcome = "ok";
  const cap = new Capture();
  try {
    await agentTxn({ http }, async (ctx) => {
      cap.txn = ctx.txn;
      await sendEmail({ to: "user@example.com", body: "hello" });
    });
  } catch (e) {
    if (e instanceof GateBlocked) outcome = "GateBlocked";
    else throw e;
  }
  void fired;
  return canonical(cap.txn!, outcome);
}

/** Holds the live txn captured from inside an agentTxn body, so a thrown
 *  GateBlocked / IsolationConflict does not lose the finished journal. */
class Capture {
  txn: { state: TxnState; effects: Effect[] } | null = null;
}

/** isolation-conflict: a read whose key is written before commit (lost update).
 *  commit() raises IsolationConflict; the txn unwinds to ROLLED_BACK. Mirrors
 *  the "read then concurrent writer" case in both languages' isolation tests. */
async function isolationConflict(): Promise<CanonJournal> {
  REGISTRY.clear();
  const db = new Database(":memory:");
  db.exec("CREATE TABLE accounts (name TEXT PRIMARY KEY, balance INTEGER NOT NULL)");
  db.prepare("INSERT INTO accounts (name, balance) VALUES (?, ?)").run("alice", 100);
  const sql = new SqliteAdapter(db);

  const readBalance = tool<{ name: string }>(
    "sql",
    (conn: SqliteDatabase, args) =>
      executeIsolated(conn, "SELECT balance FROM accounts WHERE name = ?", [args.name], {
        reads: [["accounts", args.name]],
      }),
    { name: "readBalance" },
  );

  let outcome = "ok";
  const cap = new Capture();
  try {
    await agentTxn({ sql }, async (ctx) => {
      cap.txn = ctx.txn;
      await readBalance({ name: "alice" }); // records read of alice at version 0
      // Simulate a concurrent committed write bumping alice's version.
      sql.writeVersion(["accounts", "alice"]);
    });
  } catch (e) {
    if (e instanceof IsolationConflict) outcome = "IsolationConflict";
    else throw e;
  }
  const out = canonical(cap.txn!, outcome);
  db.close();
  return out;
}

// --- per-adapter scenarios -------------------------------------------------
// One commit scenario per reversible adapter (STAGED -> APPLIED), one gate
// scenario per irreversible adapter (GATED -> ROLLED_BACK). Each drives a single
// tool call against an in-memory fake (the same fake patterns the adapter unit
// tests use). The journal captures only resource/reversible/status/keys, so the
// fakes just route the effect down the matching lane; read_keys/write_keys are
// empty on both sides. Each function mirrors a same-named Python runner.

const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

/** s3_commit — write one object that commits. */
async function s3Commit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const BUCKET = "pherix-test-bucket";
  class FakeS3 {
    store = new Map<string, Uint8Array>();
    async getObject(input: { Bucket: string; Key: string }): Promise<{ Body: Uint8Array }> {
      const v = this.store.get(input.Key);
      if (v === undefined) {
        const err = new Error("NoSuchKey") as Error & { Code: string };
        err.Code = "NoSuchKey";
        throw err;
      }
      return { Body: v };
    }
    async putObject(input: { Bucket: string; Key: string; Body: Uint8Array }): Promise<unknown> {
      this.store.set(input.Key, input.Body);
      return {};
    }
    async deleteObject(input: { Bucket: string; Key: string }): Promise<unknown> {
      this.store.delete(input.Key);
      return {};
    }
  }
  const s3 = new S3Adapter(new FakeS3(), BUCKET);
  const writeObject = tool<{ key: string }>(
    "s3",
    async (client: FakeS3, args: { key: string }) => {
      await client.putObject({ Bucket: BUCKET, Key: args.key, Body: enc("hello") });
      return { ok: true };
    },
    { name: "writeObject" },
  );
  const ctx = await agentTxn({ s3 }, async () => {
    await writeObject({ key: "doc.bin" });
  });
  return canonical(ctx.txn, "ok");
}

/** redis_commit — set one key that commits. */
async function redisCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  class FakeRedis {
    store = new Map<string, string>();
    dump(key: string): Uint8Array | null {
      const v = this.store.get(key);
      return v === undefined ? null : enc(v);
    }
    pttl(): number {
      return -1;
    }
    pipeline() {
      const ops: Array<() => void> = [];
      const self = this;
      return {
        del(key: string) {
          ops.push(() => self.store.delete(key));
        },
        restore(key: string, _ttlMs: number, serialized: Uint8Array) {
          ops.push(() => self.store.set(key, new TextDecoder().decode(serialized)));
        },
        exec() {
          for (const op of ops) op();
          return [];
        },
      };
    }
    set(key: string, value: string): void {
      this.store.set(key, value);
    }
  }
  const redis = new RedisAdapter(new FakeRedis());
  const setKey = tool<{ key: string }>(
    "redis",
    (client: FakeRedis, args: { key: string }) => {
      client.set(args.key, "hello");
      return { ok: true };
    },
    { name: "setKey" },
  );
  const ctx = await agentTxn({ redis }, async () => {
    await setKey({ key: "k" });
  });
  return canonical(ctx.txn, "ok");
}

/** mongodb_commit — insert one document that commits. */
async function mongodbCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  class FakeColl {
    docs = new Map<string, Record<string, unknown>>();
    private k(id: unknown): string {
      return JSON.stringify(id);
    }
    findOne(filter: { _id: unknown }): Record<string, unknown> | null {
      const d = this.docs.get(this.k(filter._id));
      return d ? structuredClone(d) : null;
    }
    replaceOne(filter: { _id: unknown }, replacement: Record<string, unknown>): unknown {
      this.docs.set(this.k(filter._id), structuredClone(replacement));
      return { matchedCount: 1 };
    }
    deleteOne(filter: { _id: unknown }): unknown {
      this.docs.delete(this.k(filter._id));
      return { deletedCount: 1 };
    }
    insertOne(doc: Record<string, unknown>): void {
      this.docs.set(this.k(doc["_id"]), structuredClone(doc));
    }
  }
  class FakeDb {
    private colls = new Map<string, FakeColl>();
    collection(name: string): FakeColl {
      let c = this.colls.get(name);
      if (!c) {
        c = new FakeColl();
        this.colls.set(name, c);
      }
      return c;
    }
  }
  const mongo = new MongoAdapter(new FakeDb());
  const insertDoc = tool<{ collection: string; docId: string }>(
    "mongodb",
    (db: FakeDb, args: { collection: string; docId: string }) => {
      db.collection(args.collection).insertOne({ _id: args.docId, name: "bob" });
      return { ok: true };
    },
    { name: "insertDoc" },
  );
  const ctx = await agentTxn({ mongodb: mongo }, async () => {
    await insertDoc({ collection: "users", docId: "u1" });
  });
  return canonical(ctx.txn, "ok");
}

/** dynamodb_commit — put one item that commits. */
async function dynamodbCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const TABLE = "pherix-test-table";
  class FakeDynamo {
    items = new Map<string, Record<string, unknown>>();
    async getItem(params: { TableName: string; Key: Record<string, unknown> }): Promise<{ Item?: Record<string, unknown> }> {
      const pk = (params.Key["pk"] as { S: string }).S;
      const item = this.items.get(pk);
      return item === undefined ? {} : { Item: structuredClone(item) };
    }
    async putItem(params: { TableName: string; Item: Record<string, unknown> }): Promise<unknown> {
      this.items.set((params.Item["pk"] as { S: string }).S, structuredClone(params.Item));
      return {};
    }
    async deleteItem(params: { TableName: string; Key: Record<string, unknown> }): Promise<unknown> {
      this.items.delete((params.Key["pk"] as { S: string }).S);
      return {};
    }
  }
  const ddb = new DynamoDbAdapter(new FakeDynamo(), TABLE);
  const putItem = tool<{ key: string }>(
    "dynamodb",
    async (client: FakeDynamo, args: { key: string }) => {
      await client.putItem({ TableName: TABLE, Item: { pk: { S: args.key }, v: { S: "hello" } } });
      return { ok: true };
    },
    { name: "putItem" },
  );
  const ctx = await agentTxn({ dynamodb: ddb }, async () => {
    await putItem({ key: "doc" });
  });
  return canonical(ctx.txn, "ok");
}

/** gcs_commit — save one blob that commits. */
async function gcsCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const BUCKET = "pherix-test-bucket";
  class FakeBlob {
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
  class FakeBucket {
    constructor(private readonly store: Map<string, Buffer>) {}
    file(name: string): FakeBlob {
      return new FakeBlob(this.store, name);
    }
  }
  class FakeGcs {
    private buckets = new Map<string, Map<string, Buffer>>();
    bucket(name: string): FakeBucket {
      let store = this.buckets.get(name);
      if (store === undefined) {
        store = new Map();
        this.buckets.set(name, store);
      }
      return new FakeBucket(store);
    }
  }
  const gcs = new GcsAdapter(new FakeGcs(), BUCKET);
  const saveBlob = tool<{ key: string }>(
    "gcs",
    async (client: FakeGcs, args: { key: string }) => {
      await client.bucket(BUCKET).file(args.key).save(Buffer.from("hello"));
      return { ok: true };
    },
    { name: "saveBlob" },
  );
  const ctx = await agentTxn({ gcs }, async () => {
    await saveBlob({ key: "doc.bin" });
  });
  return canonical(ctx.txn, "ok");
}

/** elasticsearch_commit — index one document that commits. */
async function elasticsearchCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const INDEX = "pherix-test-index";
  class FakeEs {
    private docs = new Map<string, Record<string, unknown>>();
    async exists(params: { index: string; id: string }): Promise<boolean> {
      return this.docs.has(params.id);
    }
    async get(params: { index: string; id: string }): Promise<{ _source: Record<string, unknown> }> {
      return { _source: structuredClone(this.docs.get(params.id)!) };
    }
    async index(params: { index: string; id: string; document: Record<string, unknown> }): Promise<unknown> {
      this.docs.set(params.id, structuredClone(params.document));
      return { result: "created" };
    }
    async delete(params: { index: string; id: string }): Promise<unknown> {
      this.docs.delete(params.id);
      return { result: "deleted" };
    }
  }
  const es = new ElasticsearchAdapter(new FakeEs(), INDEX);
  const indexDoc = tool<{ key: string }>(
    "elasticsearch",
    async (client: FakeEs, args: { key: string }) => {
      await client.index({ index: INDEX, id: args.key, document: { v: "hello" } });
      return { ok: true };
    },
    { name: "indexDoc" },
  );
  const ctx = await agentTxn({ elasticsearch: es }, async () => {
    await indexDoc({ key: "doc" });
  });
  return canonical(ctx.txn, "ok");
}

/** mysql_commit — insert one row that commits (better-sqlite3-backed fake conn,
 *  same pattern as test/mysql.test.ts: SQLite speaks the SAVEPOINT grammar). */
async function mysqlCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  function toSqlite(sql: string): string {
    let s = sql;
    s = s.replace(/\)\s*ENGINE=InnoDB/i, ")");
    s = s.replace(
      /ON DUPLICATE KEY UPDATE version = version \+ 1/i,
      "ON CONFLICT(resource, key_json) DO UPDATE SET version = version + 1",
    );
    return s;
  }
  class FakeMySql {
    constructor(public readonly db: Database.Database) {}
    async query(sql: string, params: unknown[] = []): Promise<[Array<Record<string, unknown>>, unknown]> {
      const text = toSqlite(sql);
      if (/^\s*select/i.test(text)) {
        const rows = this.db.prepare(text).all(...(params as never[])) as Array<Record<string, unknown>>;
        return [rows, undefined];
      }
      if (params.length > 0) {
        this.db.prepare(text).run(...(params as never[]));
        return [[], undefined];
      }
      this.db.exec(text);
      return [[], undefined];
    }
  }
  const db = new Database(":memory:");
  db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)");
  const mysql = new MySQLAdapter(new FakeMySql(db));
  const insertUser = tool<{ name: string }>(
    "mysql",
    async (conn: FakeMySql, args: { name: string }) => {
      await conn.query("INSERT INTO users (name) VALUES (?)", [args.name]);
      return args.name;
    },
    { name: "insertUser" },
  );
  const ctx = await agentTxn({ mysql }, async () => {
    await insertUser({ name: "bob" });
  });
  const out = canonical(ctx.txn, "ok");
  db.close();
  return out;
}

/** postgres_commit — insert one row via PostgresAdapter that commits.
 *  Uses a FakePg (better-sqlite3-backed async client) so the savepoint lane
 *  runs offline, mirroring the Python half's SQLite-backed _FakePgConn. */
async function postgresCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  class FakePg implements PgClient {
    constructor(public readonly db: Database.Database) {}
    async query(text: string, params: unknown[] = []): Promise<PgResult> {
      const sql = text.replace(/\$\d+/g, "?");
      if (/^\s*select/i.test(sql) || /\breturning\b/i.test(sql)) {
        const rows = this.db.prepare(sql).all(...(params as never[])) as Array<Record<string, unknown>>;
        return { rows };
      }
      if (params.length > 0) {
        this.db.prepare(sql).run(...(params as never[]));
        return { rows: [] };
      }
      this.db.exec(sql);
      return { rows: [] };
    }
  }
  const db = new Database(":memory:");
  db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)");
  const pg = new PostgresAdapter(new FakePg(db));
  const insertUser = tool<{ name: string }>(
    "postgres",
    (c: PgClient, args: { name: string }) => c.query("INSERT INTO users (name) VALUES ($1)", [args.name]),
    { name: "insertUser" },
  );
  const ctx = await agentTxn({ postgres: pg }, async () => {
    await insertUser({ name: "bob" });
  });
  const out = canonical(ctx.txn, "ok");
  db.close();
  return out;
}

/** git_commit — a git op against a real temp repo that commits. Guarded: the
 *  Python half skips when git is absent, so this branch is only invoked with a
 *  git binary present (same machine runs both halves). */
async function gitCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const run = (root: string, ...args: string[]): void => {
    const proc = spawnSync("git", args, { cwd: root, encoding: "utf8" });
    if (proc.status !== 0) throw new Error(`git ${args.join(" ")}: ${proc.stderr}`);
  };
  const repo = mkdtempSync(path.join(tmpdir(), "pherix_git_parity_"));
  run(repo, "init", "-q");
  run(repo, "config", "user.email", "t@example.com");
  run(repo, "config", "user.name", "t");
  writeFileSync(path.join(repo, "app.py"), "v1\n");
  run(repo, "add", "-A");
  run(repo, "commit", "-q", "-m", "c1");
  const gitAdapter = new GitAdapter(repo);
  const runGit = tool<{ command: string }>(
    "git",
    (handle: GitHandle, args: { command: string }) => handle.run(args.command),
    { name: "runGit" },
  );
  try {
    const ctx = await agentTxn({ git: gitAdapter }, async () => {
      await runGit({ command: "status" });
    });
    return canonical(ctx.txn, "ok");
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
}

/** rest_gate — a REST POST with no compensator gates at commit. */
async function restGate(): Promise<CanonJournal> {
  REGISTRY.clear();
  const calls: Array<unknown> = [];
  const transport = (method: string, url: string, opts: Record<string, unknown>): unknown => {
    calls.push({ method, url, opts });
    return { status: 201 };
  };
  const create = restTool("create_user", { method: "POST", url: "https://api/users", transport });
  let outcome = "ok";
  const cap = new Capture();
  try {
    await agentTxn({ rest: new RestAdapter() }, async (ctx) => {
      cap.txn = ctx.txn;
      await create({ json: { name: "ada" } });
    });
  } catch (e) {
    if (e instanceof GateBlocked) outcome = "GateBlocked";
    else throw e;
  }
  void calls;
  return canonical(cap.txn!, outcome);
}

/** messagequeue_gate — a publish with no compensator gates at commit. */
async function messagequeueGate(): Promise<CanonJournal> {
  REGISTRY.clear();
  const published: Array<[string, unknown]> = [];
  const broker = {
    publish(topic: string, message: unknown): unknown {
      published.push([topic, message]);
      return { acked: true };
    },
  };
  const emit = publishTool("emit_order", { broker });
  let outcome = "ok";
  const cap = new Capture();
  try {
    await agentTxn({ mq: new MqAdapter() }, async (ctx) => {
      cap.txn = ctx.txn;
      await emit({ topic: "orders", message: { id: 1 } });
    });
  } catch (e) {
    if (e instanceof GateBlocked) outcome = "GateBlocked";
    else throw e;
  }
  void published;
  return canonical(cap.txn!, outcome);
}

/** memory_commit — remember one key that commits. write_key carries a sha256
 *  version (content-addressed, not a counter), so parity verifies both the lane
 *  and the versioning scheme match across languages. */
async function memoryCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const db = new Database(":memory:");
  const mem = new MemoryAdapter(db);

  const rememberFact = tool<{ key: string; value: string }>(
    "memory",
    (handle: MemoryHandle, args: { key: string; value: string }) => {
      handle.remember(args.key, args.value);
      return { ok: true };
    },
    { name: "rememberFact" },
  );

  const ctx = await agentTxn({ memory: mem }, async () => {
    await rememberFact({ key: "greeting", value: "hello" });
  });
  db.close();
  return canonical(ctx.txn, "ok");
}

/** fs_commit — write one file in a temp directory. Exercises FilesystemAdapter's
 *  copy-on-write snapshot path. The write_key carries a sha256 of the file
 *  content, so parity also verifies the content-hash versioning scheme matches
 *  across languages. */
async function fsCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const root = mkdtempSync(path.join(tmpdir(), "pherix_fs_parity_"));
  const fs = new FilesystemAdapter(root);
  const writeFile = tool<{ content: string }>(
    "fs",
    (handle: FsHandle, args: { content: string }) => {
      handle.write("data.txt", Buffer.from(args.content));
      return { ok: true };
    },
    { name: "writeFile" },
  );
  try {
    const ctx = await agentTxn({ fs }, async () => {
      await writeFile({ content: "hello" });
    });
    return canonical(ctx.txn, "ok");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

/** dry_run_commit — a single reversible SQL write that runs then is rolled back.
 *  Effect lands as COMPENSATED (ran, snapshot restored) and the transaction ends
 *  ROLLED_BACK — the dry-run finalise path, not an error path.  Parity asserts
 *  that both languages produce the same journal: one COMPENSATED reversible, and
 *  final_state=rolled_back, outcome=ok. */
async function dryRunCommit(): Promise<CanonJournal> {
  REGISTRY.clear();
  const db = new Database(":memory:");
  db.exec("CREATE TABLE notes (body TEXT)");
  const sql = new SqliteAdapter(db);

  const insertNote = tool<{ body: string }>(
    "sql",
    (conn: SqliteDatabase, args: { body: string }) => {
      conn.prepare("INSERT INTO notes (body) VALUES (?)").run(args.body);
      return { ok: true };
    },
    { name: "insertNote" },
  );

  try {
    const ctx = await dryRun({ sql }, async () => {
      await insertNote({ body: "hello" });
    });
    return canonical(ctx.txn, "ok");
  } finally {
    db.close();
  }
}

const SCENARIOS: Record<string, () => Promise<CanonJournal>> = {
  reversible_commit: reversibleCommit,
  irreversible_gate: irreversibleGate,
  isolation_conflict: isolationConflict,
  // EXTENSION POINT: add one entry per newly-ported TS adapter here, mirroring
  // the matching Python runner in tests/test_sdk_parity.py's SCENARIOS list.
  // Reversible adapters — one commit scenario each (STAGED -> APPLIED).
  s3_commit: s3Commit,
  redis_commit: redisCommit,
  mongodb_commit: mongodbCommit,
  dynamodb_commit: dynamodbCommit,
  gcs_commit: gcsCommit,
  elasticsearch_commit: elasticsearchCommit,
  mysql_commit: mysqlCommit,
  postgres_commit: postgresCommit,
  git_commit: gitCommit,
  fs_commit: fsCommit,
  // Irreversible adapters — one gate scenario each (GATED -> ROLLED_BACK).
  rest_gate: restGate,
  messagequeue_gate: messagequeueGate,
  // Memory adapter — reversible commit with write_key (content-addressed sha256).
  memory_commit: memoryCommit,
  // Dry-run path — reversible write runs and is rolled back; COMPENSATED + ROLLED_BACK.
  dry_run_commit: dryRunCommit,
};

async function main(): Promise<void> {
  const name = process.argv[2];
  const fn = SCENARIOS[name ?? ""];
  if (fn === undefined) {
    process.stderr.write(`unknown scenario ${JSON.stringify(name)}\n`);
    process.exit(2);
  }
  const journal = await fn();
  process.stdout.write(JSON.stringify(journal));
}

await main();
