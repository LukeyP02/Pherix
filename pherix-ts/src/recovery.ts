/**
 * Crash-consistent recovery — resume an interrupted backward fold (#9).
 * Mirror of pherix/core/recovery.py.
 *
 * The runtime's mixed-fold unwind already folds the journal backward when a
 * commit fails inside one live process. The gap this closes is the next failure
 * along the timeline: the process *dies* part-way through that unwind (or before
 * it started), leaving a transaction in a non-terminal durable state with real
 * side effects still standing.
 *
 * The model is unchanged — everything is a traversal of the journal. Recovery is
 * the backward fold again, driven from the *durable* journal rather than the
 * in-memory Transaction (which died with the process): re-read the persisted
 * effect rows, re-derive what still needs undoing, resume the fold to a terminal
 * state.
 *
 * What survives a crash, precisely:
 *
 * - Uncommitted reversible (SQL) writes do NOT need recovery. A SAVEPOINT is
 *   connection-local and dies with the process — but so does the enclosing
 *   BEGIN, and the database auto-rolls-back any uncommitted transaction the
 *   instant the connection closes. So a reversible effect that was APPLIED but
 *   not committed is already undone by the DB before recovery runs. Recovery
 *   records this honestly (COMPENSATED, "the DB already did it") and does NOT
 *   ROLLBACK TO a savepoint that no longer exists.
 * - APPLIED irreversible effects are the real target. They left the process and
 *   changed the outside world; that side effect persisted, and so did the journal
 *   row. The compensator — the semantic left-inverse — is the only thing that can
 *   undo it. Recovery re-fires it with the journalled args.
 *
 * Exactly-once: the durable effect status is the fence. An irreversible is
 * compensated iff its row reads APPLIED; the moment its compensator fires
 * successfully the row flips to COMPENSATED and is committed. A second pass sees
 * COMPENSATED and skips it — the compensator never runs twice.
 */

import Database from "better-sqlite3";
import type { ResourceAdapter } from "./adapters/base.js";
import type { AuditJournal } from "./audit.js";
import { Effect, EffectStatus } from "./effects.js";
import { REGISTRY, ToolRegistry } from "./tools.js";
import { TxnState } from "./transaction.js";

// A transaction is a recovery candidate when its durable state is non-terminal
// AND it owns at least one APPLIED effect — the proof that real work happened
// the unwind has not yet reversed. (Statuses/states are the lowercase TS enum
// values, the form the audit journal persists.)
const RECOVERABLE_STATES: readonly string[] = [
  TxnState.OPEN,
  TxnState.STAGED,
  TxnState.PARTIAL,
  TxnState.STUCK,
];

/** What recovery did to one effect during the resumed fold. */
export interface EffectRecovery {
  effectId: string;
  index: number;
  tool: string;
  reversible: boolean;
  // "compensated" | "db_auto_rolled_back" | "already_compensated"
  // | "stuck_missing_compensator" | "stuck_compensator_raised"
  action: string;
  error?: string;
}

/** Outcome of resuming the backward fold for one transaction. */
export class TxnRecovery {
  constructor(
    readonly txnId: string,
    readonly priorState: string,
    readonly finalState: string,
    readonly effects: EffectRecovery[],
  ) {}

  get compensatorsFired(): number {
    return this.effects.filter((e) => e.action === "compensated").length;
  }
}

/** Aggregate outcome of a recovery sweep over a durable journal. */
export class RecoveryReport {
  constructor(readonly transactions: TxnRecovery[]) {}

  get recovered(): number {
    return this.transactions.filter((t) => t.finalState === TxnState.ROLLED_BACK).length;
  }
  get stuck(): number {
    return this.transactions.filter((t) => t.finalState === TxnState.STUCK).length;
  }
  get compensatorsFired(): number {
    return this.transactions.reduce((n, t) => n + t.compensatorsFired, 0);
  }
}

interface EffectRow {
  idx: number;
  effect_id: string;
  tool: string;
  resource: string;
  reversible: number;
  status: string;
  args: string;
}

function setEffectStatus(db: Database.Database, txnId: string, idx: number, status: string): void {
  // better-sqlite3 runs each statement in autocommit (no open transaction), so
  // this UPDATE is durable immediately — that per-effect commit is what makes
  // the fence crash-safe: a crash mid-recovery leaves every already-undone
  // effect durably COMPENSATED, so the next pass skips it.
  db.prepare("UPDATE effects SET status = ? WHERE txn_id = ? AND idx = ?").run(status, txnId, idx);
}

function setTxnState(db: Database.Database, txnId: string, state: string): void {
  db.prepare("UPDATE transactions SET state = ?, updated_at = ? WHERE txn_id = ?").run(
    state,
    new Date().toISOString(),
    txnId,
  );
}

function findMidFlight(db: Database.Database): Array<{ txn_id: string; state: string }> {
  const placeholders = RECOVERABLE_STATES.map(() => "?").join(",");
  return db
    .prepare(
      `SELECT t.txn_id, t.state FROM transactions t ` +
        `WHERE t.state IN (${placeholders}) ` +
        `AND EXISTS (SELECT 1 FROM effects e WHERE e.txn_id = t.txn_id AND e.status = ?) ` +
        `ORDER BY t.created_at`,
    )
    .all(...RECOVERABLE_STATES, EffectStatus.APPLIED) as Array<{ txn_id: string; state: string }>;
}

async function resumeOne(
  db: Database.Database,
  txnId: string,
  priorState: string,
  adapters: Record<string, ResourceAdapter>,
  registry: ToolRegistry,
): Promise<TxnRecovery> {
  const rows = db
    .prepare("SELECT * FROM effects WHERE txn_id = ? ORDER BY idx DESC")
    .all(txnId) as EffectRow[];

  const outcomes: EffectRecovery[] = [];
  let stuck = false;

  for (const row of rows) {
    const reversible = row.reversible !== 0;

    if (row.status === EffectStatus.COMPENSATED) {
      // Already undone (prior pass or pre-crash unwind). The fence in action.
      outcomes.push({
        effectId: row.effect_id,
        index: row.idx,
        tool: row.tool,
        reversible,
        action: "already_compensated",
      });
      continue;
    }

    if (row.status !== EffectStatus.APPLIED) {
      // STAGED never fired; FAILED was the trigger; GATED was denied. Nothing
      // standing in the world to undo.
      continue;
    }

    if (reversible) {
      // The DB already undid the uncommitted write on process death. Record the
      // fact honestly; do not touch a dead savepoint.
      setEffectStatus(db, txnId, row.idx, EffectStatus.COMPENSATED);
      outcomes.push({
        effectId: row.effect_id,
        index: row.idx,
        tool: row.tool,
        reversible: true,
        action: "db_auto_rolled_back",
      });
      continue;
    }

    // Irreversible APPLIED — the real target. Resolve the compensator from the
    // registry by the effect's tool name.
    const compName = registry.has(row.tool) ? registry.get(row.tool).compensator : null;
    if (compName === null || !registry.has(compName)) {
      stuck = true;
      outcomes.push({
        effectId: row.effect_id,
        index: row.idx,
        tool: row.tool,
        reversible: false,
        action: "stuck_missing_compensator",
        error: `tool ${JSON.stringify(row.tool)} has no registered compensator; the standing side effect requires manual recovery`,
      });
      continue;
    }

    const compSpec = registry.get(compName);
    const compAdapter = adapters[compSpec.resource];
    if (compAdapter === undefined) {
      stuck = true;
      outcomes.push({
        effectId: row.effect_id,
        index: row.idx,
        tool: row.tool,
        reversible: false,
        action: "stuck_missing_compensator",
        error: `no adapter registered for resource ${JSON.stringify(compSpec.resource)}`,
      });
      continue;
    }

    // Synthetic carrier for the compensator fire — re-invoke with the original
    // args, exactly as the live unwind does. Not journalled as a separate row.
    const compEffect = new Effect({
      txnId,
      index: -1,
      tool: compName,
      args: JSON.parse(row.args) as Record<string, unknown>,
      resource: compSpec.resource,
      reversible: false,
      effectId: `comp-${row.effect_id}`,
    });
    try {
      await compAdapter.apply(compEffect, compSpec.fn);
    } catch (exc) {
      // Any compensator failure means the inverse did NOT apply: the row stays
      // APPLIED and the txn is STUCK. Swallow so other txns still recover.
      stuck = true;
      outcomes.push({
        effectId: row.effect_id,
        index: row.idx,
        tool: row.tool,
        reversible: false,
        action: "stuck_compensator_raised",
        error: exc instanceof Error ? `${exc.name}: ${exc.message}` : String(exc),
      });
      continue;
    }

    // Inverse applied. Flip + commit the fence BEFORE moving on, so a crash
    // right here cannot re-fire this compensator on the next pass.
    setEffectStatus(db, txnId, row.idx, EffectStatus.COMPENSATED);
    outcomes.push({
      effectId: row.effect_id,
      index: row.idx,
      tool: row.tool,
      reversible: false,
      action: "compensated",
    });
  }

  const final = stuck ? TxnState.STUCK : TxnState.ROLLED_BACK;
  setTxnState(db, txnId, final);
  return new TxnRecovery(txnId, priorState, final, outcomes);
}

/**
 * Resume every interrupted backward fold in a durable journal — the public
 * entry point. Given the durable audit journal (an AuditJournal, or a path to
 * its SQLite file — the "fresh process after a crash" case), the live adapter
 * map, and the tool registry, find every mid-flight transaction and resume its
 * backward fold to a terminal state.
 *
 * Idempotent by construction: re-running against the same DB is a no-op for any
 * transaction whose effects are already terminal — the durable status is the
 * exactly-once fence.
 *
 * Recovery must run in a process that has re-registered its tools (so
 * compensators resolve from `registry`) and re-attached its adapters (so the
 * inverse can actually fire). Defaults to the process-global REGISTRY.
 */
export async function recover(
  journal: AuditJournal | string,
  adapters: Record<string, ResourceAdapter>,
  registry: ToolRegistry = REGISTRY,
): Promise<RecoveryReport> {
  // Reuse the caller's connection (an AuditJournal) or open our own to the
  // durable file (the post-crash case). Only close a connection we opened.
  const ownConnection = typeof journal === "string";
  const db: Database.Database = ownConnection ? new Database(journal) : journal.connection;
  try {
    const midFlight = findMidFlight(db);
    const reports: TxnRecovery[] = [];
    for (const row of midFlight) {
      reports.push(await resumeOne(db, row.txn_id, row.state, adapters, registry));
    }
    return new RecoveryReport(reports);
  } finally {
    if (ownConnection) db.close();
  }
}
