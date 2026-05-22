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
import type { ToolFn, TransactionalResourceAdapter } from "./base.js";

/** The slice of the better-sqlite3 Database surface this adapter uses. */
export interface SqliteDatabase {
  exec(source: string): unknown;
  prepare(source: string): { run(...params: any[]): unknown; get(...params: any[]): unknown };
}

export class SqliteAdapter implements TransactionalResourceAdapter {
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
}
