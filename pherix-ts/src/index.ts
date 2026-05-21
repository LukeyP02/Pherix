/**
 * Pherix — the interception axis, in TypeScript.
 *
 * A transactional resource runtime for AI agents: wrap your tool-call layer in
 * `agentTxn`, register tools with `tool`, and reversible effects become
 * journalled-and-undoable while irreversible ones are staged and gated — all
 * under a policy, on an append-only journal. Semantic mirror of the Python
 * library; see README.md for the parity note.
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
export { isTransactionalAdapter } from "./adapters/base.js";
export type {
  ResourceAdapter,
  TransactionalResourceAdapter,
  ToolFn,
} from "./adapters/base.js";
export { SqliteAdapter } from "./adapters/sql.js";
export type { SqliteDatabase } from "./adapters/sql.js";
export { HttpAdapter, IrreversibleAdapterError } from "./adapters/http.js";

// Policy
export {
  Policy,
  PolicyContext,
  PolicyRule,
  PolicyViolation,
  Cap,
  Allow,
  Deny,
} from "./policy.js";
export type { Verdict, RuleFn, NamedRule, Where, PolicyInit } from "./policy.js";

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
