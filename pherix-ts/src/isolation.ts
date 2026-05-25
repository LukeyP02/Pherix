/**
 * Isolation policies and the commit-time conflict diff (#8).
 * Mirror of pherix/core/isolation.py — in-process, single-host tier.
 *
 * Maths framing: the journal is a time series of observables; a conflict is a
 * non-commutativity event between this transaction's read set
 * {(resource, key, vRead)} and the committed mutations on those same keys since
 * the transaction opened. The resolution policy is a function
 * `f: Conflict[] -> Action`, with Action drawn from {Abort, Serialize}.
 *
 * The diff fires only at commit time — per-effect checking would need a global
 * lock over every operation. Reads within a transaction are isolated by the
 * journal's append-only semantics (a transaction reads its own writes,
 * untouched by others until commit), so there is no TOCTOU window inside a txn,
 * only at the commit boundary.
 *
 * Concurrency model — the TS divergence. Python coordinates concurrent agents
 * with `threading.Event`s; Node has no threads — its concurrent agents are
 * async tasks interleaving at await boundaries. So `Serialize` here is
 * async-native: it `await`s while any in-flight peer transaction (in the same
 * process) plans a write to a key this transaction read. The cross-*process*
 * intent ledger and the `Retry`-via-run-loop policy are deliberately deferred
 * (single-host SQLite hack + the #12 control plane, exactly as Python defers
 * cross-host) — wire them when a TS user runs cross-process agents.
 */

import type { ResourceAdapter } from "./adapters/base.js";
import type { Effect, ReadKey, WriteKey } from "./effects.js";

/** One non-commutativity event. `versionExpected` is what the diff actually
 *  compared `versionNow` against — `versionAtRead` on the committed-only path,
 *  or the version after my last own write on the own-write-visible path. The
 *  conflict fired because `versionNow !== versionExpected`. */
export interface Conflict {
  resource: string;
  key: unknown;
  versionAtRead: unknown;
  versionNow: unknown;
  versionExpected: unknown;
}

/** Raised when the commit-time diff fires under the Abort policy. The list of
 *  conflicting keys is carried so an enclosing handler can inspect what moved. */
export class IsolationConflict extends Error {
  readonly conflicts: Conflict[];
  constructor(conflicts: Conflict[]) {
    const lines = conflicts
      .map(
        (c) =>
          `${c.resource}:${JSON.stringify(c.key)} (read v${JSON.stringify(c.versionAtRead)}, ` +
          `expected v${JSON.stringify(c.versionExpected)}, now v${JSON.stringify(c.versionNow)})`,
      )
      .join("; ");
    super(`isolation conflict on ${lines}`);
    this.name = "IsolationConflict";
    this.conflicts = [...conflicts];
  }
}

// --- resolution policies -----------------------------------------------------

/** A resolution policy maps a set of conflicts to an action. The runtime runs
 *  the diff unconditionally at commit; on a non-empty conflict set it calls
 *  `resolve`. The pluggable hook is what keeps the engine general — the policy
 *  is not baked into the fold. */
export interface IsolationPolicy {
  /** Pre-diff wait, if any (Serialize). Default: no wait. */
  waitTimeoutSeconds?: number;
  resolve(ctx: unknown, conflicts: Conflict[]): void;
}

/** Default: raise IsolationConflict; the txn unwinds normally. The trivial
 *  `f: Conflict -> "give up and tell the caller"`. The caller decides whether
 *  to retry; Pherix does not. */
export class Abort implements IsolationPolicy {
  resolve(_ctx: unknown, conflicts: Conflict[]): void {
    throw new IsolationConflict(conflicts);
  }
}

/** Block this commit until no other in-flight (same-process) txn plans a write
 *  to any of our reads, or the timeout expires. The waiting is driven by the
 *  runtime BEFORE the diff fires; by the time `resolve` is called the wait has
 *  finished AND a conflict still exists on the post-wait diff — so this is the
 *  unhappy-path fallback, which degrades to Abort. */
export class Serialize implements IsolationPolicy {
  readonly waitTimeoutSeconds: number;
  constructor(timeoutSeconds = 30) {
    this.waitTimeoutSeconds = timeoutSeconds;
  }
  resolve(_ctx: unknown, conflicts: Conflict[]): void {
    throw new IsolationConflict(conflicts);
  }
}

// --- the diff ----------------------------------------------------------------

/** A versioned adapter exposes the current version of a key for the diff. */
interface VersionedAdapter extends ResourceAdapter {
  readVersion(key: unknown): unknown;
  readsCommittedOnly?(): boolean;
}

function isVersioned(adapter: ResourceAdapter | undefined): adapter is VersionedAdapter {
  return adapter !== undefined && typeof (adapter as VersionedAdapter).readVersion === "function";
}

function keyId(resource: string, key: unknown): string {
  return `${resource}::${JSON.stringify(key)}`;
}

/**
 * Fold the journal: for every read key, compare the version we expect against
 * the version the adapter reports now. Emit a Conflict when those differ.
 *
 * Effects whose adapter is non-rollback (e.g. the HTTP adapter) or non-versioned
 * are skipped — irreversibles are isolated-by-construction via staging, so their
 * reads do not participate in MVCC.
 *
 * Self-bump disambiguation: when a txn reads a key then writes it, the adapter's
 * version moves because of OUR own write. The expected-current per key is the
 * version after my LAST write of that key (`lastMyWrite`), or `vAtRead` if I
 * only read it. A live version beyond that means a cross-txn write — a genuine
 * lost-update. On a committed-only adapter (whose readVersion excludes my own
 * uncommitted writes) the comparison is instead `vAtRead` vs `vNow`, since my
 * self-bumps cancel on both ends; an adapter signals that via
 * `readsCommittedOnly()` (default false → own-write-visible).
 */
export function checkConflicts(
  effects: Effect[],
  adapters: Record<string, ResourceAdapter>,
): Conflict[] {
  // Per (resource, key): the version produced by my LAST write (append-order,
  // so the last write_keys entry for a key is the freshest).
  const lastMyWrite = new Map<string, unknown>();
  for (const effect of effects) {
    for (const [resource, key, vAfter] of effect.writeKeys as WriteKey[]) {
      lastMyWrite.set(keyId(resource, key), vAfter);
    }
  }

  const conflicts: Conflict[] = [];
  for (const effect of effects) {
    for (const [resource, key, vAtRead] of effect.readKeys as ReadKey[]) {
      const adapter = adapters[resource];
      if (!isVersioned(adapter) || !adapter.supportsRollback()) continue;
      const committedOnly =
        typeof adapter.readsCommittedOnly === "function" ? adapter.readsCommittedOnly() : false;
      const id = keyId(resource, key);
      const vExpected = committedOnly ? vAtRead : (lastMyWrite.get(id) ?? vAtRead);
      const vNow = adapter.readVersion(key);
      if (vNow !== vExpected) {
        conflicts.push({
          resource,
          key,
          versionAtRead: vAtRead,
          versionNow: vNow,
          versionExpected: vExpected,
        });
      }
    }
  }
  return conflicts;
}

// --- in-process arbitration substrate (the registry) -------------------------

interface RegisteredCtx {
  readonly txnId: string;
  readonly txn: { effects: Effect[] };
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/**
 * In-process registry of open transactions for Serialize. Each TxnContext
 * registers on open and unregisters on close. Serialize consults it to find
 * other in-flight txns whose write_keys intersect this txn's read_keys and
 * `await`s their completion before the commit-time diff.
 *
 * Single lock-free model: Node is single-threaded, so a synchronous snapshot of
 * `open` is consistent; the only suspension points are the `await`s in
 * `waitForBlockers`, between which the map can change (a peer commits and
 * unregisters) — which is exactly what lets the wait make progress.
 */
export class JournalRegistry {
  private readonly open = new Map<string, RegisteredCtx>();
  private static readonly POLL_MS = 5;

  register(ctx: RegisteredCtx): void {
    this.open.set(ctx.txnId, ctx);
  }

  unregister(ctx: RegisteredCtx): void {
    this.open.delete(ctx.txnId);
  }

  openContexts(): RegisteredCtx[] {
    return [...this.open.values()];
  }

  /** Resolve once no other in-flight txn plans a write to any of `myReadKeys`,
   *  or the timeout expires. After return the caller re-runs `checkConflicts`:
   *  a wait that wakes via timeout still falls through to the diff, which either
   *  finds the world quiet (proceed) or still moving (Conflict under Serialize,
   *  degraded to Abort). */
  async waitForBlockers(
    myTxnId: string,
    myReadKeys: ReadKey[],
    timeoutSeconds: number,
  ): Promise<void> {
    const myReads = new Set(myReadKeys.map(([r, k]) => keyId(r, k)));
    if (myReads.size === 0) return;
    const deadline = Date.now() + timeoutSeconds * 1000;
    for (;;) {
      let blocked = false;
      for (const [txnId, ctx] of this.open) {
        if (txnId === myTxnId) continue;
        for (const effect of ctx.txn.effects) {
          for (const [r, k] of effect.writeKeys as WriteKey[]) {
            if (myReads.has(keyId(r, k))) {
              blocked = true;
              break;
            }
          }
          if (blocked) break;
        }
        if (blocked) break;
      }
      if (!blocked) return;
      const remaining = deadline - Date.now();
      if (remaining <= 0) return;
      // Yield so peers can make progress and unregister; re-check after.
      await sleep(Math.min(JournalRegistry.POLL_MS, remaining));
    }
  }
}

/** Process-global singleton. The runtime's agentTxn registers/unregisters
 *  automatically; tests that register manually must unregister. */
export const REGISTRY = new JournalRegistry();
