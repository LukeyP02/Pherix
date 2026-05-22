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

import type { Effect, SnapshotHandle } from "../effects.js";
import type { StateDiffable, ToolFn, TransactionalResourceAdapter } from "./base.js";

/** The slice of the better-sqlite3 Database surface this adapter uses. */
export interface SqliteDatabase {
  exec(source: string): unknown;
  prepare(source: string): {
    run(...params: any[]): unknown;
    get(...params: any[]): unknown;
    all(...params: any[]): unknown[];
  };
}

export class SqliteAdapter implements TransactionalResourceAdapter, StateDiffable {
  readonly name = "sql";
  private readonly db: SqliteDatabase;

  constructor(db: SqliteDatabase) {
    this.db = db;
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

  // --- state diff (StateDiffable) — for dry-run preview --------------------

  /** Names of user-created tables, excluding SQLite's own internal catalogue. */
  private userTables(): string[] {
    const rows = this.db
      .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
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
