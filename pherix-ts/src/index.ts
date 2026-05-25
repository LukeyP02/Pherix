/**
 * Pherix — the interception axis, in TypeScript.
 *
 * A transactional resource runtime for AI agents: wrap your tool-call layer in
 * `agentTxn`, register tools with `tool`, and reversible effects become
 * journalled-and-undoable while irreversible ones are staged and gated — all
 * under a policy, on an append-only journal.
 *
 * Parity scope: this is a faithful semantic mirror of the Python library's
 * core lanes — interception, the reversible/irreversible split, the
 * twice-evaluated policy (allow/deny + caps + the human gate), and the audit
 * journal. The TS SDK now also carries: the SQL (SQLite), filesystem, Postgres,
 * and irreversible (HTTP) adapters; the vetted compensator catalog
 * (payments / identity / provisioning / SaaS); world-state-aware policy (#7)
 * with the twice-evaluated TOCTOU divergence; and speculative dry-run
 * (`dryRun` + `DryRunResult`, with per-adapter state diff). Because async DB
 * drivers (node-postgres) have no synchronous query API, the adapter lifecycle
 * is awaitable here — the one deliberate divergence from Python's synchronous
 * psycopg lane.
 *
 * The hardened engine tier is now mirrored too: commit-time isolation with the
 * conflict diff + Abort/Serialize policies (#8, in-process tier), crash-
 * consistent recovery that resumes the backward fold from the durable journal
 * (#9), and the durable longitudinal envelope of cross-run spend caps (#10).
 *
 * Still deliberately deferred (single-host hacks + the #12 control plane, which
 * Python itself defers cross-host): the cross-*process* intent ledger and the
 * Retry-via-run-loop for #8, and hard cross-process budget enforcement for #10.
 * The MCP gateway and deterministic replay are not part of the TS SDK's job.
 *
 * Tool calls are async: the agent `await`s every registered-tool call so an
 * async tool (the normal TS case) is fully resolved before its effect is
 * marked APPLIED, and a rejection drives FAILED + unwind rather than escaping.
 */

// Effects + journal
export {
  Effect,
  EffectStatus,
  EffectArgsError,
  StagedResult,
  computeEffectId,
  canonicalJson,
} from "./effects.js";
export type { EffectInit, SnapshotHandle, ReadKey, WriteKey } from "./effects.js";

// Transaction state machine
export { Transaction, TxnState, TransactionStateError, newTxnId } from "./transaction.js";

// Tools + interception
export { tool, REGISTRY, ToolRegistry, activeTxn, activeEffect } from "./tools.js";
export type { ToolSpec, ToolOptions, ToolWrapper, RecordingContext } from "./tools.js";

// Adapters — the whole axis comes through the barrel
export * from "./adapters/index.js";

// Policy
export {
  Policy,
  PolicyContext,
  PolicyRule,
  PolicyViolation,
  Cap,
  Allow,
  Deny,
  sqlReader,
  refundIfPaid,
} from "./policy.js";
export { PolicyVerdict } from "./policy.js";
export type {
  Verdict,
  RuleFn,
  NamedRule,
  Where,
  PolicyInit,
  ReadMediator,
  RefundIfPaidOptions,
} from "./policy.js";

// Audit journal
export { AuditJournal } from "./audit.js";
export type { TransactionRow, EffectRow } from "./audit.js";

// Runtime orchestration
export {
  agentTxn,
  TxnContext,
  GateBlocked,
  CompensatorNotRegistered,
} from "./runtime.js";
export type { AgentTxnOptions, TxnContextOptions } from "./runtime.js";

// Dry-run — speculative execution (fold forward, then discard)
export { dryRun, DryRunResult } from "./dry-run.js";
export type { DryRunOptions } from "./dry-run.js";

// Longitudinal envelope — durable, cross-run spend caps (#10)
export {
  EnvelopeStore,
  DurableCap,
  dayPeriod,
  allTimePeriod,
  isDurableCap,
  pendingIncrements,
  flushIncrements,
} from "./envelope.js";
export type { PeriodFn, EnvelopeIncrement } from "./envelope.js";

// Crash-consistent recovery — resume an interrupted backward fold (#9)
export { recover, RecoveryReport, TxnRecovery } from "./recovery.js";
export type { EffectRecovery } from "./recovery.js";

// Isolation — commit-time conflict diff + resolution policies (#8)
export {
  checkConflicts,
  Abort,
  Serialize,
  IsolationConflict,
  JournalRegistry,
  REGISTRY as ISOLATION_REGISTRY,
} from "./isolation.js";
export type { Conflict, IsolationPolicy } from "./isolation.js";

// Compensator catalog — vetted semantic left-inverses
export * from "./compensators/index.js";
