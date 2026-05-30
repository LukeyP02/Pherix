/**
 * SqliteAdapter — the reversible lane proved against a real backend.
 * Mirror of pherix/core/adapters/sql.py, over the `better-sqlite3` driver.
 *
 * `snapshot()` issues a real `SAVEPOINT`; `restore()` does `ROLLBACK TO
 * SAVEPOINT`. The database does the heavy lifting — rollback is correct by
 * construction, needing no knowledge of what the effect meant. The whole
 * transaction is bracketed by `begin()` / `commit()` / `rollback()`.
 *
 * The driver type is kept structural (a minimal interface) so this module does
 * not force `better-sqlite3`'s types on consumers and so tests can substitute a
 * compatible fake if ever needed. At runtime the real driver satisfies it.
 */

import { activeEffect } from "../tools.js";
import type { Effect, SnapshotHandle } from "../effects.js";
import type { StateDiffable, ToolFn, TransactionalResourceAdapter } from "./base.js";

/** The slice of the better-sqlite3 Database surface this adapter uses. */
export interface SqliteDatabase {
  exec(source: string): unknown;
  prepare(source: string): {
    run(...params: any[]): unknown;
    get(...params: any[]): unknown;
    all(...params: any[]): unknown[];
    reader?: boolean;
  };
}

// Version side-table — the isolation substrate (#8). One monotonic counter per
// (resource, key). Created idempotently so the first readVersion of an unknown
// key returns 0.
const VERSIONS_TABLE_DDL = `
CREATE TABLE IF NOT EXISTS _pherix_versions (
    resource TEXT NOT NULL,
    key_json TEXT NOT NULL,
    version  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, key_json)
)`;

/** Maps a live connection back to the adapter that wraps it, so the free
 *  function `executeIsolated(conn, ...)` can reach readVersion/writeVersion
 *  without changing the SQL tool calling convention. Mirrors Python's
 *  `_adapter_for(conn)`. */
const ADAPTER_FOR = new WeakMap<object, SqliteAdapter>();

export class SqliteAdapter implements TransactionalResourceAdapter, StateDiffable {
  readonly name = "sql";
  private readonly db: SqliteDatabase;
  /** Committed-only read connection (never inside a BEGIN). Present only for
   *  on-disk databases; null for `:memory:`. When present, readVersion routes
   *  through this connection so cross-process commits are visible at
   *  commit-time even though the main connection's snapshot was taken earlier. */
  private readonly metaDb: SqliteDatabase | null;

  constructor(db: SqliteDatabase, opts: { metaDb?: SqliteDatabase } = {}) {
    this.db = db;
    this.metaDb = opts.metaDb ?? null;
    // Create the version side-table eagerly (idempotent) so the first
    // readVersion on an unknown key returns 0, not a missing-table error.
    this.db.exec(VERSIONS_TABLE_DDL);
    ADAPTER_FOR.set(db as object, this);
  }

  /** The underlying connection, for tools that need it directly in tests. */
  get connection(): SqliteDatabase {
    return this.db;
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
    // index is internal (journal position), never user input — safe to inline.
    return `sp_${Math.trunc(index)}`;
  }

  snapshot(effect: Effect): SnapshotHandle {
    const sp = SqliteAdapter.savepointName(effect.index);
    this.db.exec(`SAVEPOINT ${sp}`);
    return { resource: this.name, effectIndex: effect.index, payload: { savepoint: sp } };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // SQL tools receive the connection as their injected handle, then the
    // named-args object — the mirror of Python's tool_fn(conn, **args).
    return toolFn(this.db, effect.args);
  }

  restore(handle: SnapshotHandle): void {
    const sp = handle.payload["savepoint"] as string;
    // Rolls back to the savepoint, not beyond it: the parent txn stays open.
    this.db.exec(`ROLLBACK TO SAVEPOINT ${sp}`);
  }

  // --- versioning (isolation substrate, #8) --------------------------------

  private static encodeKey(key: unknown): string {
    // The key arrives as an array (e.g. ["accounts", "alice"]); JSON is its
    // canonical, stable encoding for the side-table primary key.
    return JSON.stringify(key);
  }

  /** Whether readVersion reflects ONLY committed state (excludes this txn's own
   *  uncommitted writes). True when a metaDb (committed-only connection) was
   *  provided — on-disk databases with cross-process isolation. False for the
   *  single-connection in-process path (`:memory:` or no metaDb supplied):
   *  reads see our own bumps, so the diff uses the own-write-visible branch. */
  readsCommittedOnly(): boolean {
    return this.metaDb !== null;
  }

  /** Current version of `key`. Absent row → 0 ("never written"); never null.
   *  Uses metaDb when present so the read bypasses the main connection's BEGIN
   *  snapshot and sees the latest committed state from any process. */
  readVersion(key: unknown): number {
    const target = this.metaDb ?? this.db;
    const row = target
      .prepare("SELECT version FROM _pherix_versions WHERE resource = ? AND key_json = ?")
      .get(this.name, SqliteAdapter.encodeKey(key)) as { version: number } | undefined;
    return row === undefined ? 0 : Number(row.version);
  }

  /** Atomically bump `key`'s version; return the new value. */
  writeVersion(key: unknown): number {
    const row = this.db
      .prepare(
        "INSERT INTO _pherix_versions (resource, key_json, version) VALUES (?, ?, 1) " +
          "ON CONFLICT(resource, key_json) DO UPDATE SET version = version + 1 RETURNING version",
      )
      .get(this.name, SqliteAdapter.encodeKey(key)) as { version: number };
    return Number(row.version);
  }

  // --- state diff (StateDiffable) — for dry-run preview --------------------

  /** Names of user-created tables, excluding SQLite's own internal catalogue
   *  and Pherix's version side-table (bookkeeping, not user state). */
  private userTables(): string[] {
    const rows = this.db
      .prepare(
        "SELECT name FROM sqlite_master WHERE type = 'table' " +
          "AND name NOT LIKE 'sqlite_%' AND name != '_pherix_versions'",
      )
      .all() as Array<{ name: string }>;
    return rows.map((r) => r.name);
  }

  /** `{rowid: rowJson}` for one table, keyed by the stable implicit rowid so
   *  update-vs-insert can be told apart without parsing the schema. `table`
   *  comes from sqlite_master, never user input, so interpolating it is safe by
   *  construction (SQLite cannot parameterise identifiers regardless). */
  private dumpTable(table: string): Record<number, string> {
    const rows = this.db.prepare(`SELECT rowid AS __rowid, * FROM ${table}`).all() as Array<
      Record<string, unknown>
    >;
    const out: Record<number, string> = {};
    for (const row of rows) {
      const { __rowid, ...rest } = row;
      out[__rowid as number] = JSON.stringify(rest);
    }
    return out;
  }

  stateBaseline(): Record<string, Record<number, string>> {
    const out: Record<string, Record<number, string>> = {};
    for (const t of this.userTables()) out[t] = this.dumpTable(t);
    return out;
  }

  stateDiff(baseline: unknown): Record<string, unknown> {
    const base = baseline as Record<string, Record<number, string>>;
    const added: Array<{ table: string; row: unknown }> = [];
    const modified: Array<{ table: string; row: unknown }> = [];
    const deleted: Array<{ table: string; row: unknown }> = [];
    const liveTables = this.userTables();
    for (const table of liveTables) {
      const before = base[table] ?? {};
      const now = this.dumpTable(table);
      for (const [rowid, rowJson] of Object.entries(now)) {
        if (!(rowid in before)) added.push({ table, row: JSON.parse(rowJson) });
        else if (before[rowid as unknown as number] !== rowJson) {
          modified.push({ table, row: JSON.parse(rowJson) });
        }
      }
      for (const [rowid, rowJson] of Object.entries(before)) {
        if (!(rowid in now)) deleted.push({ table, row: JSON.parse(rowJson) });
      }
    }
    // A table present in the baseline but dropped during the txn: its rows
    // count as deletions so the diff stays honest about lost data.
    const liveSet = new Set(liveTables);
    for (const [table, before] of Object.entries(base)) {
      if (!liveSet.has(table)) {
        for (const rowJson of Object.values(before)) {
          deleted.push({ table, row: JSON.parse(rowJson) });
        }
      }
    }
    return { rows_added: added, rows_modified: modified, rows_deleted: deleted };
  }
}

/**
 * SQL execution that records read/write keys into the active Effect (#8).
 * Mirror of pherix.core.adapters.sql.execute_isolated.
 *
 * `reads` and `writes` are the keys this statement touches — each a tuple-shaped
 * array like `["accounts", "alice"]`. SQL parsing is out of scope, so tools
 * declare which rows they touched explicitly; the journal shape is the same as
 * if it were derived from the statement.
 *
 * Always runs the statement and returns its result (rows for a reader, the
 * run-info otherwise). Inside an agentTxn it records each read as
 * `["sql", key, readVersion(key)]` into the effect's readKeys (deduped) and each
 * write as `["sql", key, writeVersion(key)]` into writeKeys (after bumping the
 * side-table version — the version-after-my-write the commit diff compares
 * against). Outside an agentTxn (no active effect) or for a bare connection not
 * wrapped by a SqliteAdapter, recording is skipped — the statement still runs.
 */
export function executeIsolated(
  conn: SqliteDatabase,
  stmt: string,
  params: unknown[] = [],
  opts: { reads?: unknown[]; writes?: unknown[] } = {},
): unknown {
  const prepared = conn.prepare(stmt);
  const result = prepared.reader ? prepared.all(...params) : prepared.run(...params);

  const effect = activeEffect.getStore();
  const adapter = ADAPTER_FOR.get(conn as object);
  if (effect === undefined || adapter === undefined) return result;

  const seenReads = new Set(effect.readKeys.map(([, k]) => JSON.stringify(k)));
  for (const key of opts.reads ?? []) {
    const enc = JSON.stringify(key);
    if (seenReads.has(enc)) continue;
    effect.readKeys.push(["sql", key, adapter.readVersion(key)]);
    seenReads.add(enc);
  }
  for (const key of opts.writes ?? []) {
    // write_keys carries (resource, key, versionAfterMyWrite) so the diff can
    // disambiguate self-bumps from cross-txn writes. Not deduped: repeated
    // writes append fresh triples; the diff picks the freshest by order.
    const vAfter = adapter.writeVersion(key);
    effect.writeKeys.push(["sql", key, vAfter]);
  }
  return result;
}
