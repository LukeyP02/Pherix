/**
 * MySQLAdapter — the reversible adapter for MySQL / MariaDB, async.
 * Mirror of pherix/core/adapters/mysql.py, over the `mysql2/promise` driver
 * shape.
 *
 * Same shape as SqliteAdapter / PostgresAdapter: the database does the heavy
 * lifting. `snapshot` issues a real `SAVEPOINT`, `restore` does `ROLLBACK TO
 * SAVEPOINT` — correct by construction, so `supportsRollback()` is `true`. The
 * whole transaction is bracketed by `begin()` / `commit()` / `rollback()`.
 *
 * Engine requirement. Savepoints and transactional rollback require a
 * transactional storage engine — InnoDB (the MySQL default). DDL is *not*
 * transactional and implicitly commits, so the savepoint/restore lane covers
 * DML (rows), not schema changes — same as the SQLite/Postgres adapters.
 *
 * Connection contract. The adapter drives every BEGIN / SAVEPOINT / COMMIT /
 * ROLLBACK itself, so the connection must be in autocommit mode — otherwise the
 * driver opens an implicit transaction that fights this adapter's explicit one.
 * This mirrors SQLite's "the adapter controls every transaction boundary".
 *
 * Async by necessity. mysql2's promise API has no synchronous query form, so
 * every lifecycle method returns a `Promise`; the runtime awaits them (see
 * adapters/base.ts). The version side-table is created lazily on first use
 * (Python creates it eagerly in __init__, but a synchronous constructor cannot
 * await the async DDL — so we ensure-once on the async paths instead, which is
 * observably identical to a caller).
 *
 * No RETURNING. MySQL lacks `INSERT ... RETURNING`, so `writeVersion` does the
 * atomic upsert (`INSERT ... ON DUPLICATE KEY UPDATE`) then `SELECT`s the new
 * value back within the same transaction — race-free because the row is locked
 * by the upsert until the txn boundary.
 *
 * The driver is kept structural (`MySQLConnection`) so this module forces no
 * `mysql2` types on consumers and tests can substitute a compatible fake (an
 * in-memory SQLite-backed connection speaks the same SAVEPOINT / ROLLBACK TO
 * grammar, giving an offline savepoint-restore proof).
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ToolFn, TransactionalResourceAdapter } from "./base.js";

/** The slice of the mysql2/promise Connection surface this adapter uses:
 *  a single async `query(sql, params?)` returning `[rows, fields]` where
 *  `rows` is an array of row objects (mysql2's RowDataPacket shape). */
export interface MySQLConnection {
  query(sql: string, params?: unknown[]): Promise<[Array<Record<string, unknown>>, unknown]>;
}

// Side-table holding monotonic version counters per (resource, key) — the
// isolation substrate. `key_json` is the canonical JSON encoding of the key
// tuple. InnoDB is required for the transactional savepoint lane and is the
// default engine. Key columns use a bounded prefix length because TEXT cannot
// be a primary key in MySQL without one; VARCHAR(255) is ample.
const VERSIONS_TABLE_DDL = `
CREATE TABLE IF NOT EXISTS _pherix_versions (
    resource VARCHAR(255) NOT NULL,
    key_json VARCHAR(255) NOT NULL,
    version  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, key_json)
) ENGINE=InnoDB`;

export class MySQLAdapter implements TransactionalResourceAdapter {
  readonly name = "mysql";
  private readonly conn: MySQLConnection;
  private versionsTableReady = false;

  constructor(connection: MySQLConnection) {
    this.conn = connection;
  }

  get connection(): MySQLConnection {
    return this.conn;
  }

  supportsRollback(): boolean {
    return true;
  }

  /** Create the version side-table once (idempotent DDL). Async because the
   *  driver is; the eager Python __init__ is folded into first-use here. */
  private async ensureVersionsTable(): Promise<void> {
    if (this.versionsTableReady) return;
    await this.conn.query(VERSIONS_TABLE_DDL);
    this.versionsTableReady = true;
  }

  // --- transaction-scope lifecycle (driven by the runtime) ---

  async begin(): Promise<void> {
    await this.conn.query("BEGIN");
  }

  async commit(): Promise<void> {
    await this.conn.query("COMMIT");
  }

  async rollback(): Promise<void> {
    await this.conn.query("ROLLBACK");
  }

  // --- per-effect snapshot / apply / restore ---

  private static savepointName(index: number): string {
    // index is internal (journal position), never user input — safe to inline.
    // MySQL cannot parameterise identifiers regardless.
    return `sp_${Math.trunc(index)}`;
  }

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const sp = MySQLAdapter.savepointName(effect.index);
    await this.conn.query(`SAVEPOINT ${sp}`);
    return { resource: this.name, effectIndex: effect.index, payload: { savepoint: sp } };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The txn-owned connection is injected as the tool's first arg; the @tool
    // wrapper hides it from the agent's call-site. The tool awaits the async
    // query; the runtime awaits apply.
    return toolFn(this.conn, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const sp = handle.payload["savepoint"] as string;
    await this.conn.query(`ROLLBACK TO SAVEPOINT ${sp}`);
  }

  // --- versioning (isolation substrate) ----------------------------------

  private static encodeKey(key: unknown): string {
    // The key arrives as an array (e.g. ["users", 1]); JSON is its canonical,
    // stable cross-process encoding for the side-table primary key. Mirrors
    // Python's json.dumps(list(key), sort_keys=True).
    return JSON.stringify(key);
  }

  /** Current version of `key`. Absent row → 0 ("never written"); never null. */
  async readVersion(key: unknown): Promise<number> {
    await this.ensureVersionsTable();
    const [rows] = await this.conn.query(
      "SELECT version FROM _pherix_versions WHERE resource = ? AND key_json = ?",
      [this.name, MySQLAdapter.encodeKey(key)],
    );
    const row = rows[0];
    return row === undefined ? 0 : Number(row["version"]);
  }

  /** Atomically bump `key`'s version; return the new value. MySQL has no
   *  RETURNING, so upsert then SELECT the new value back; the upsert's row lock
   *  is held until the txn boundary, keeping the read-back race-free. */
  async writeVersion(key: unknown): Promise<number> {
    await this.ensureVersionsTable();
    const keyJson = MySQLAdapter.encodeKey(key);
    await this.conn.query(
      "INSERT INTO _pherix_versions (resource, key_json, version) VALUES (?, ?, 1) " +
        "ON DUPLICATE KEY UPDATE version = version + 1",
      [this.name, keyJson],
    );
    const [rows] = await this.conn.query(
      "SELECT version FROM _pherix_versions WHERE resource = ? AND key_json = ?",
      [this.name, keyJson],
    );
    return Number((rows[0] as Record<string, unknown>)["version"]);
  }
}
