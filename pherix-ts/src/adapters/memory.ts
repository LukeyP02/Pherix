/**
 * MemoryAdapter — governed agent memory as just another resource.
 * Mirror of pherix/core/adapters/memory.py.
 *
 * Rollback is correct-by-construction via SQLite SAVEPOINTs (same as
 * SqliteAdapter). Versioning is content-addressed via SHA-256 (same as
 * FilesystemAdapter): a key's version is the sha256 of its current value, or
 * the `__missing__` sentinel when absent. Durability comes from the SQLite
 * file persisting committed state across runs.
 *
 * The `better-sqlite3`-typed `SqliteDatabase` structural interface is reused
 * so the adapter does not force a hard dependency on the real driver; tests
 * can swap in any conforming object.
 */

import { createHash } from "node:crypto";
import type { Effect, SnapshotHandle } from "../effects.js";
import type { StateDiffable, ToolFn, TransactionalResourceAdapter } from "./base.js";
import type { SqliteDatabase } from "./sql.js";
import { activeEffect } from "../tools.js";

const MEMORY_TABLE_DDL = `
CREATE TABLE IF NOT EXISTS _pherix_memory (
    namespace TEXT NOT NULL,
    mem_key   TEXT NOT NULL,
    value     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    PRIMARY KEY (namespace, mem_key)
)`;

/** Sentinel version for a key that has never been remembered. */
const MEM_MISSING = "__missing__";

function sha256Hex(data: string): string {
  return createHash("sha256").update(data, "utf8").digest("hex");
}

function nowIso(): string {
  return new Date().toISOString();
}

/**
 * Per-effect handle injected as the first arg of memory tools (hidden from the
 * agent's call-site, exactly as the SQL connection is hidden). Speaks the
 * memory vocabulary — `remember` / `recall` / `forget` — and records read/write
 * keys into the active Effect so isolation and audit fall out for free.
 */
export class MemoryHandle {
  private readonly recordedReads = new Set<string>();

  constructor(
    private readonly db: SqliteDatabase,
    private readonly namespace: string,
    private readonly effect: Effect | null,
    private readonly adapter: MemoryAdapter | null,
  ) {}

  /** Persist `value` under `key` (UPSERT). A journalled write. */
  remember(key: string, value: unknown): void {
    const payload = typeof value === "string" ? value : JSON.stringify(value);
    this.db
      .prepare(
        "INSERT INTO _pherix_memory (namespace, mem_key, value, ts) VALUES (?, ?, ?, ?) " +
          "ON CONFLICT(namespace, mem_key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
      )
      .run(this.namespace, key, payload, nowIso());
    this._recordWriteKey(key);
  }

  /** Return the value remembered under `key`, or null if absent. A read. */
  recall(key: string): string | null {
    const row = this.db
      .prepare("SELECT value FROM _pherix_memory WHERE namespace = ? AND mem_key = ?")
      .get(this.namespace, key) as { value: string } | undefined;
    this._recordReadKey(key);
    return row === undefined ? null : row.value;
  }

  /** Delete `key` from memory. A journalled write; absent key is a no-op. */
  forget(key: string): void {
    this.db
      .prepare("DELETE FROM _pherix_memory WHERE namespace = ? AND mem_key = ?")
      .run(this.namespace, key);
    this._recordWriteKey(key);
  }

  private _recordReadKey(key: string): void {
    if (this.effect === null || this.adapter === null) return;
    if (this.recordedReads.has(key)) return;
    const version = this.adapter.readVersion([key]);
    this.effect.readKeys.push(["memory", [key], version]);
    this.recordedReads.add(key);
  }

  private _recordWriteKey(key: string): void {
    // Re-hash AFTER the write lands so the recorded version matches what
    // readVersion would report now — the commit-time diff's expected current.
    if (this.effect === null || this.adapter === null) return;
    const versionAfter = this.adapter.readVersion([key]);
    this.effect.writeKeys.push(["memory", [key], versionAfter]);
  }
}

/**
 * ResourceAdapter over a durable, namespaced key/value memory store.
 *
 * The savepoint lane (begin/commit/rollback/snapshot/restore) is identical to
 * SqliteAdapter. Versioning is content-addressed (SHA-256), not a monotonic
 * integer counter, so the commit-time isolation diff flags "I read this memory,
 * someone else rewrote it" without a counter side-table.
 */
export class MemoryAdapter implements TransactionalResourceAdapter, StateDiffable {
  readonly name = "memory";
  private readonly db: SqliteDatabase;
  private readonly namespace: string;

  constructor(db: SqliteDatabase, options: { namespace?: string } = {}) {
    this.db = db;
    this.namespace = options.namespace ?? "default";
    // Idempotent DDL — re-binding to the same file is safe.
    this.db.exec(MEMORY_TABLE_DDL);
  }

  supportsRollback(): boolean {
    return true;
  }

  begin(): void {
    this.db.exec("BEGIN");
  }

  commit(): void {
    this.db.exec("COMMIT");
  }

  rollback(): void {
    this.db.exec("ROLLBACK");
  }

  private static savepointName(index: number): string {
    return `mem_sp_${Math.trunc(index)}`;
  }

  snapshot(effect: Effect): SnapshotHandle {
    const sp = MemoryAdapter.savepointName(effect.index);
    this.db.exec(`SAVEPOINT ${sp}`);
    return { resource: this.name, effectIndex: effect.index, payload: { savepoint: sp } };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    const handle = new MemoryHandle(
      this.db,
      this.namespace,
      activeEffect.getStore() ?? null,
      this,
    );
    return toolFn(handle, effect.args);
  }

  restore(handle: SnapshotHandle): void {
    const sp = handle.payload["savepoint"] as string;
    this.db.exec(`ROLLBACK TO SAVEPOINT ${sp}`);
  }

  // --- versioning (content-addressed, mirror of FilesystemAdapter) ----------

  /**
   * Current version of `key` (a 1-element array `[memKey]`). Returns the
   * sha256 hex of the stored value, or `__missing__` when absent.
   */
  readVersion(key: [string]): string {
    if (key.length !== 1) {
      throw new Error(
        `MemoryAdapter version key must be a 1-element array [memKey]; got ${JSON.stringify(key)}`,
      );
    }
    const row = this.db
      .prepare("SELECT value FROM _pherix_memory WHERE namespace = ? AND mem_key = ?")
      .get(this.namespace, key[0]) as { value: string } | undefined;
    return row === undefined ? MEM_MISSING : sha256Hex(row.value);
  }

  /** Post-write version — same as readVersion (re-computed from stored value). */
  writeVersion(key: [string]): string {
    return this.readVersion(key);
  }

  // --- state diff (StateDiffable — dry-run structural delta) -----------------

  private dump(): Record<string, string> {
    const rows = this.db
      .prepare("SELECT mem_key, value FROM _pherix_memory WHERE namespace = ?")
      .all(this.namespace) as Array<{ mem_key: string; value: string }>;
    const out: Record<string, string> = {};
    for (const r of rows) out[r.mem_key] = r.value;
    return out;
  }

  stateBaseline(): Record<string, string> {
    return this.dump();
  }

  stateDiff(baseline: unknown): Record<string, unknown> {
    const base = baseline as Record<string, string>;
    const now = this.dump();
    const keysAdded: string[] = [];
    const keysModified: string[] = [];
    const keysDeleted: string[] = [];
    for (const k of Object.keys(now)) {
      if (!(k in base)) keysAdded.push(k);
      else if (base[k] !== now[k]) keysModified.push(k);
    }
    for (const k of Object.keys(base)) {
      if (!(k in now)) keysDeleted.push(k);
    }
    return { keys_added: keysAdded, keys_modified: keysModified, keys_deleted: keysDeleted };
  }
}
