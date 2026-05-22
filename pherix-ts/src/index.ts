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
 * twice-evaluated policy (allow/deny + caps + the human gate), the SQL +
 * irreversible adapters, compensators, and the audit journal. The hardened
 * engine features added to Python after this branch's base (world-state
 * policy, cross-process isolation, crash-consistent recovery, the longitudinal
 * envelope) are NOT yet mirrored and are tracked as follow-ups. The `readKeys`
 * / `writeKeys` slots exist on `Effect` for shape parity, but the isolation
 * conflict engine is not wired here.
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

// Adapters
export { isTransactionalAdapter, isStateDiffable } from "./adapters/base.js";
export type {
  ResourceAdapter,
  TransactionalResourceAdapter,
  StateDiffable,
  ToolFn,
} from "./adapters/base.js";
export { SqliteAdapter } from "./adapters/sql.js";
export type { SqliteDatabase } from "./adapters/sql.js";
export { HttpAdapter, IrreversibleAdapterError } from "./adapters/http.js";
export { FilesystemAdapter, FsHandle } from "./adapters/fs.js";
export { PostgresAdapter } from "./adapters/postgres.js";
export type { PgClient, PgResult } from "./adapters/postgres.js";

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

// Compensator catalog — vetted semantic left-inverses
export * from "./compensators/index.js";
