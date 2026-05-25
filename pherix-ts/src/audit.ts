/**
 * Audit journal — append-only SQLite persistence of the effect journal.
 * Mirror of pherix/core/audit.py, over the `better-sqlite3` driver.
 *
 * A separate SQLite database, two tables (`transactions` + `effects`). Args,
 * snapshot and result are stored as JSON; effect `status` is updated in place.
 * There are no deletes — the journal *is* the audit log.
 *
 * Status / state are stored as the lowercase enum value (the TS canonical
 * form); this is the only cosmetic divergence from the Python rows, which use
 * the uppercase enum *name*. Per-language journals are independent — the
 * guarantee is the append-only structure, not byte-identical strings.
 */

import Database from "better-sqlite3";
import { canonicalJson, type Effect } from "./effects.js";
import type { Transaction } from "./transaction.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS transactions (
    txn_id        TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    replayed_from TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    client_id     TEXT
);
CREATE TABLE IF NOT EXISTS effects (
    txn_id     TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    effect_id  TEXT NOT NULL,
    tool       TEXT NOT NULL,
    resource   TEXT NOT NULL,
    reversible INTEGER NOT NULL,
    status     TEXT NOT NULL,
    args       TEXT NOT NULL,
    snapshot   TEXT,
    result     TEXT,
    read_keys  TEXT NOT NULL DEFAULT '[]',
    write_keys TEXT NOT NULL DEFAULT '[]',
    ts         TEXT NOT NULL,
    PRIMARY KEY (txn_id, idx)
);
`;

function dump(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return canonicalJson(value);
}

function now(): string {
  return new Date().toISOString();
}

export interface TransactionRow {
  txn_id: string;
  state: string;
  created_at: string;
  updated_at: string;
  replayed_from: string | null;
  dry_run: number;
  client_id: string | null;
}

export interface EffectRow {
  txn_id: string;
  idx: number;
  effect_id: string;
  tool: string;
  resource: string;
  reversible: number;
  status: string;
  args: string;
  snapshot: string | null;
  result: string | null;
  read_keys: string;
  write_keys: string;
  ts: string;
}

/** SQLite-backed audit journal — a persistent transcript of every effect. */
export class AuditJournal {
  private readonly db: Database.Database;

  constructor(path: string) {
    this.db = new Database(path);
    this.db.exec(SCHEMA);
  }

  /** Construct an in-memory (non-durable) journal — for tests / ephemeral runs.
   *  Mirrors Python's explicit `AuditJournal.in_memory()`: the durability
   *  choice is visible at the call site, not hidden in a default argument. */
  static inMemory(): AuditJournal {
    return new AuditJournal(":memory:");
  }

  close(): void {
    this.db.close();
  }

  /** The underlying connection, for siblings in the same durability domain
   *  (e.g. the longitudinal EnvelopeStore's totals table lives in this same
   *  SQLite file — one host, one DB). The deliberate seam Python exposes as
   *  `audit._conn`; not for foreign consumers. */
  get connection(): Database.Database {
    return this.db;
  }

  // --- transactions ---

  recordTransaction(
    txn: Transaction,
    opts: { dryRun?: boolean; clientId?: string | null } = {},
  ): void {
    const ts = now();
    this.db
      .prepare(
        "INSERT INTO transactions " +
          "(txn_id, state, created_at, updated_at, replayed_from, dry_run, client_id) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?)",
      )
      .run(
        txn.txnId,
        txn.state,
        ts,
        ts,
        txn.replayedFrom,
        opts.dryRun ? 1 : 0,
        opts.clientId ?? null,
      );
  }

  updateTransactionState(txnId: string, state: string): void {
    this.db
      .prepare("UPDATE transactions SET state = ?, updated_at = ? WHERE txn_id = ?")
      .run(state, now(), txnId);
  }

  getTransaction(txnId: string): TransactionRow | null {
    const row = this.db
      .prepare("SELECT * FROM transactions WHERE txn_id = ?")
      .get(txnId) as TransactionRow | undefined;
    return row ?? null;
  }

  // --- effects ---

  recordEffect(effect: Effect): void {
    this.db
      .prepare(
        "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, " +
          "reversible, status, args, snapshot, result, read_keys, write_keys, ts) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      )
      .run(
        effect.txnId,
        effect.index,
        effect.effectId,
        effect.tool,
        effect.resource,
        effect.reversible ? 1 : 0,
        effect.status,
        dump(effect.args),
        dump(effect.snapshot),
        dump(effect.result),
        dump(effect.readKeys) ?? "[]",
        dump(effect.writeKeys) ?? "[]",
        effect.ts.toISOString(),
      );
  }

  /** Update mutable state in place — same row, no history. */
  updateEffect(effect: Effect): void {
    this.db
      .prepare(
        "UPDATE effects SET status = ?, snapshot = ?, result = ?, " +
          "read_keys = ?, write_keys = ? WHERE txn_id = ? AND idx = ?",
      )
      .run(
        effect.status,
        dump(effect.snapshot),
        dump(effect.result),
        dump(effect.readKeys) ?? "[]",
        dump(effect.writeKeys) ?? "[]",
        effect.txnId,
        effect.index,
      );
  }

  getEffects(txnId: string): EffectRow[] {
    return this.db
      .prepare("SELECT * FROM effects WHERE txn_id = ? ORDER BY idx")
      .all(txnId) as EffectRow[];
  }
}
