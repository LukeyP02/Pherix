/**
 * PostgresAdapter — the reversible lane proved against a real backend, async.
 * Mirror of pherix/core/adapters/postgres.py, over the `pg` (node-postgres)
 * driver shape.
 *
 * Same shape as SqliteAdapter: the database does the heavy lifting.
 * `snapshot()` issues a real `SAVEPOINT`; `restore()` does `ROLLBACK TO
 * SAVEPOINT` — correct by construction, so `supportsRollback()` is `true`.
 * The whole transaction is bracketed by `begin()` / `commit()` / `rollback()`.
 *
 * Async by necessity. node-postgres has no synchronous query API — every query
 * is a promise — so every lifecycle method here returns a `Promise`. The
 * runtime awaits them (see adapters/base.ts). This is the one place the TS lane
 * genuinely diverges from Python's synchronous psycopg lane, and the reason the
 * adapter protocol was generalised to be awaitable.
 *
 * The driver type is kept structural (`PgClient`) so this module does not force
 * `pg`'s types on consumers and so tests can substitute a compatible fake (an
 * in-memory SQLite-backed client speaks the same SAVEPOINT / ROLLBACK TO
 * grammar, giving an offline savepoint-restore proof). At runtime a real
 * node-postgres `Client`/`PoolClient` satisfies it.
 *
 * Connection contract. The adapter drives every BEGIN / SAVEPOINT / COMMIT /
 * ROLLBACK itself, so the supplied client must not be inside an implicit
 * surrounding transaction — exactly the discipline SqliteAdapter keeps (there
 * via `isolation_level=None`; here the client is used in its default
 * autocommit-per-statement mode and this adapter owns every boundary).
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ToolFn, TransactionalResourceAdapter } from "./base.js";

/** The result shape node-postgres returns from `query`. Only `rows` is used. */
export interface PgResult {
  rows: Array<Record<string, unknown>>;
}

/** The slice of the node-postgres `Client` surface this adapter uses:
 *  a single async `query(text, params?)` returning `{ rows }`. */
export interface PgClient {
  query(text: string, params?: unknown[]): Promise<PgResult>;
}

export class PostgresAdapter implements TransactionalResourceAdapter {
  readonly name = "postgres";
  private readonly client: PgClient;

  constructor(client: PgClient) {
    this.client = client;
  }

  /** The underlying client, for tools and world-state reads. */
  get connection(): PgClient {
    return this.client;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- transaction-scope lifecycle (driven by the runtime) ---

  async begin(): Promise<void> {
    await this.client.query("BEGIN");
  }

  async commit(): Promise<void> {
    await this.client.query("COMMIT");
  }

  async rollback(): Promise<void> {
    await this.client.query("ROLLBACK");
  }

  private static savepointName(index: number): string {
    // index is internal (journal position), never user input — safe to inline.
    // Postgres cannot parameterise identifiers regardless.
    return `sp_${Math.trunc(index)}`;
  }

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const sp = PostgresAdapter.savepointName(effect.index);
    await this.client.query(`SAVEPOINT ${sp}`);
    return { resource: this.name, effectIndex: effect.index, payload: { savepoint: sp } };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // SQL tools receive the client as their injected handle, then the
    // named-args object — the mirror of Python's tool_fn(conn, **args). The
    // tool itself awaits the client's async query; the runtime awaits apply.
    return toolFn(this.client, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const sp = handle.payload["savepoint"] as string;
    // Rolls back to the savepoint, not beyond it: the parent txn stays open.
    await this.client.query(`ROLLBACK TO SAVEPOINT ${sp}`);
  }
}
