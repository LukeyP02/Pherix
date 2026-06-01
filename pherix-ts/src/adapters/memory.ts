/**
 * MemoryAdapter — governed agent memory as just another resource.
 * Mirror of pherix/core/adapters/memory.py, over the `better-sqlite3` driver.
 *
 * The north star says "governed memory" is NOT a new axis: an agent's memory is
 * a resource placed under the same envelope as any other — an *adapter + a
 * policy*. This module proves that by satisfying the existing ResourceAdapter
 * protocol against a durable key/value memory store, with no engine surgery.
 * `remember` / `recall` / `forget` become ordinary journalled effects:
 * reversible, policy-governed, audited, rollback-able — exactly like a SQL write
 * or a file write.
 *
 * A deliberate hybrid that makes it its own adapter, not a re-skin of either
 * neighbour:
 *
 * - Rollback is correct-by-construction via SQLite savepoints, like
 *   `SqliteAdapter`: `snapshot` issues a real `SAVEPOINT`, `restore` does
 *   `ROLLBACK TO SAVEPOINT`, and the txn bracket is a plain BEGIN / COMMIT /
 *   ROLLBACK. The database does the undo — so a rolled-back `remember` simply
 *   never happened, and `recall` in a later transaction cannot see it.
 * - Versioning is content-addressed, like `FilesystemAdapter`: a key's version
 *   is the sha256 of its current value (or the `__missing__` sentinel when
 *   absent), so the commit-time isolation diff flags "I read this memory,
 *   someone else rewrote it" without a counter side-table.
 *
 * Durability comes from the SQLite file persisting committed state across runs
 * and processes — open a fresh adapter on the same path and `recall` returns
 * what a prior transaction committed.
 *
 * The driver type reuses the structural `SqliteDatabase` slice from sql.ts, so
 * this module does not force `better-sqlite3`'s types on consumers. The DB must
 * be in autocommit mode (the better-sqlite3 default) so this adapter — not the
 * driver's implicit machinery — owns every BEGIN / SAVEPOINT / COMMIT /
 * ROLLBACK, exactly as the SQL adapter requires.
 */

import { createHash } from "node:crypto";
import { activeEffect } from "../tools.js";
import type { Effect, SnapshotHandle } from "../effects.js";
import type { SqliteDatabase } from "./sql.js";
import type { StateDiffable, ToolFn, TransactionalResourceAdapter } from "./base.js";

// The single durable store table. `namespace` scopes one agent's memory from
// another's; `mem_key` is the lookup key within a namespace. `value` is the
// remembered payload (JSON text). One adapter instance binds one namespace.
const MEMORY_TABLE_DDL = `
CREATE TABLE IF NOT EXISTS _pherix_memory (
    namespace TEXT NOT NULL,
    mem_key   TEXT NOT NULL,
    value     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    PRIMARY KEY (namespace, mem_key)
)`;

// Sentinel returned by `readVersion` for a key that has never been remembered.
// A non-null marker means the commit-time isolation diff can tell "I recalled
// this as absent" apart from a real content hash via a plain `!==` — a later
// `remember` of the same key then correctly flags a conflict.
const MEM_MISSING = "__missing__";

/**
 * The per-effect memory handle injected as the first arg of memory tools.
 *
 * The `tool` wrapper hides it from the agent's call-site, exactly as the SQL
 * connection and the filesystem `FsHandle` are hidden. It speaks the memory
 * vocabulary — `remember` / `recall` / `forget` — and records read/write keys
 * into the active Effect so isolation and audit fall out for free. Recording is
 * a no-op when `effect` is null (the handle still works for raw unit tests
 * outside `agentTxn`).
 */
export class MemoryHandle {
  private readonly recordedReads = new Set<string>();

  constructor(
    private readonly db: SqliteDatabase,
    private readonly namespace: string,
    private readonly effect: Effect | null,
    private readonly adapter: MemoryAdapter | null,
  ) {}

  // --- public API (tool-facing) ---

  /** Persist `value` under `key` (UPSERT). A journalled write. */
  remember(key: string, value: unknown): void {
    const payload = typeof value === "string" ? value : JSON.stringify(value);
    this.db
      .prepare(
        "INSERT INTO _pherix_memory (namespace, mem_key, value, ts) VALUES (?, ?, ?, ?) " +
          "ON CONFLICT(namespace, mem_key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
      )
      .run(this.namespace, key, payload, now());
    this.recordWriteKey(key);
  }

  /**
   * Return the value remembered under `key`, or null if absent.
   *
   * A read: it records a read-key but never a write-key, so a memory policy that
   * forbids writes leaves `recall` untouched — recall is read-only by
   * construction, not by a special rule.
   */
  recall(key: string): string | null {
    const row = this.db
      .prepare("SELECT value FROM _pherix_memory WHERE namespace = ? AND mem_key = ?")
      .get(this.namespace, key) as { value: string } | undefined;
    this.recordReadKey(key);
    return row === undefined ? null : row.value;
  }

  /** Delete `key` from memory. A journalled write; absent key is a no-op. */
  forget(key: string): void {
    this.db
      .prepare("DELETE FROM _pherix_memory WHERE namespace = ? AND mem_key = ?")
      .run(this.namespace, key);
    this.recordWriteKey(key);
  }

  // --- isolation recording ---

  private recordReadKey(key: string): void {
    if (this.effect === null || this.adapter === null) return;
    if (this.recordedReads.has(key)) return;
    const version = this.adapter.readVersion([key]);
    this.effect.readKeys.push(["memory", [key], version]);
    this.recordedReads.add(key);
  }

  private recordWriteKey(key: string): void {
    // Re-hash AFTER the write lands so the recorded version is the one
    // `readVersion` would report now — the commit-time diff's "expected current"
    // for this key. Writes are not deduplicated; the diff folds to the freshest.
    if (this.effect === null || this.adapter === null) return;
    const versionAfter = this.adapter.readVersion([key]);
    this.effect.writeKeys.push(["memory", [key], versionAfter]);
  }
}

export class MemoryAdapter implements TransactionalResourceAdapter, StateDiffable {
  readonly name = "memory";
  private readonly db: SqliteDatabase;
  private readonly ns: string;

  constructor(db: SqliteDatabase, options: { namespace?: string } = {}) {
    this.db = db;
    this.ns = options.namespace ?? "default";
    // Idempotent DDL — re-binding the adapter to the same file is safe. Runs in
    // autocommit (before any begin()), so the table exists for the first txn.
    this.db.exec(MEMORY_TABLE_DDL);
  }

  /** The underlying connection — exposed so the world-state read mediator can
   *  serve `ctx.read("memory", ...)` rules through the unchanged runtime, the
   *  same way the SQL adapter's connection is reached. */
  get connection(): SqliteDatabase {
    return this.db;
  }

  get namespace(): string {
    return this.ns;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- transaction-scope lifecycle (savepoint lane, shared with SQL) ---

  begin(): void {
    this.db.exec("BEGIN");
  }

  commit(): void {
    this.db.exec("COMMIT");
  }

  rollback(): void {
    this.db.exec("ROLLBACK");
  }

  // The `mem_sp` prefix distinguishes memory savepoints from the SQL adapter's
  // `sp_` so two adapters sharing one connection cannot collide on a name.
  private static savepointName(index: number): string {
    // index is internal (journal position), never user input — safe to inline.
    return `mem_sp_${Math.trunc(index)}`;
  }

  snapshot(effect: Effect): SnapshotHandle {
    const sp = MemoryAdapter.savepointName(effect.index);
    this.db.exec(`SAVEPOINT ${sp}`);
    return { resource: this.name, effectIndex: effect.index, payload: { savepoint: sp } };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // D2: the txn-owned handle is injected as the tool's first arg; the tool
    // wrapper hides it from the agent's call-site, then passes the named-args
    // object. `activeEffect` is set by the runtime around adapter.apply; outside
    // an agentTxn it is undefined and the handle skips recording.
    const handle = new MemoryHandle(this.db, this.ns, activeEffect.getStore() ?? null, this);
    return toolFn(handle, effect.args);
  }

  restore(handle: SnapshotHandle): void {
    const sp = handle.payload["savepoint"] as string;
    // Rolls back to the savepoint, not beyond it: the parent txn stays open.
    this.db.exec(`ROLLBACK TO SAVEPOINT ${sp}`);
  }

  // --- versioning (content-addressed, like the filesystem adapter) ---

  readVersion(key: unknown[]): string {
    if (key.length !== 1) {
      throw new Error(
        `MemoryAdapter version key must be a 1-tuple (memKey,); got ${JSON.stringify(key)}`,
      );
    }
    const row = this.db
      .prepare("SELECT value FROM _pherix_memory WHERE namespace = ? AND mem_key = ?")
      .get(this.ns, key[0]) as { value: string } | undefined;
    if (row === undefined) return MEM_MISSING;
    return createHash("sha256").update(row.value, "utf8").digest("hex");
  }

  writeVersion(key: unknown[]): string {
    // Recomputed from the stored value after the write — no cache.
    return this.readVersion(key);
  }

  // --- state diff (StateDiffable — dry-run structural delta) ---

  /** `{memKey: value}` for this namespace — a read-only snapshot. */
  private dump(): Record<string, string> {
    const rows = this.db
      .prepare("SELECT mem_key, value FROM _pherix_memory WHERE namespace = ?")
      .all(this.ns) as Array<{ mem_key: string; value: string }>;
    const out: Record<string, string> = {};
    for (const r of rows) out[r.mem_key] = r.value;
    return out;
  }

  stateBaseline(): Record<string, string> {
    return this.dump();
  }

  stateDiff(baseline: unknown): Record<string, unknown> {
    const base = baseline as Record<string, string>;
    const live = this.dump();
    const added = Object.keys(live).filter((k) => !(k in base));
    const modified = Object.keys(live).filter((k) => k in base && base[k] !== live[k]);
    const deleted = Object.keys(base).filter((k) => !(k in live));
    return { keys_added: added, keys_modified: modified, keys_deleted: deleted };
  }
}

function now(): string {
  return new Date().toISOString();
}
