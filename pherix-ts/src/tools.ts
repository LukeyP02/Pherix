/**
 * tool() registration + registry — transparent interception.
 * Mirror of pherix/core/tools.py.
 *
 * A registered tool returns a wrapper that checks ambient async-local state for
 * an active transaction. Inside `agentTxn()` the call is journalled and routed
 * through an adapter; outside, the wrapper is a transparent passthrough that
 * runs the raw function un-journalled. The agent loop and tool call-sites are
 * never transaction-aware — there is no explicit `txn.call()` API.
 *
 * Python uses ContextVar; the faithful Node equivalent is AsyncLocalStorage —
 * ambient state that survives `await` boundaries without leaking across
 * concurrent async contexts.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { Effect } from "./effects.js";
import type { ToolFn } from "./adapters/base.js";

/** What the tool wrapper needs from the active transaction — kept minimal so
 *  tools.ts never imports runtime.ts (that would be an import cycle). */
export interface RecordingContext {
  recordToolCall(toolName: string, args: Record<string, unknown>): unknown;
}

/** Set by runtime.agentTxn(). Holds the active transaction context or null. */
export const activeTxn = new AsyncLocalStorage<RecordingContext>();

/**
 * Set by the runtime around `adapter.apply(effect, toolFn)`. Holds the Effect
 * whose readKeys / writeKeys are the recording target for the currently
 * executing tool call — the hook isolation bookkeeping would read.
 */
export const activeEffect = new AsyncLocalStorage<Effect>();

export interface ToolSpec {
  name: string;
  fn: ToolFn;
  resource: string;
  reversible: boolean;
  /** First param (e.g. the SQL connection) is supplied by the adapter at apply
   *  time and hidden from the agent's call-site. */
  injectsHandle: boolean;
  /** Name of another registered tool that is this tool's semantic left-inverse.
   *  Resolved at fire-time; missing names fail loudly at stage-time. Pherix does
   *  not verify the inverse property — the developer asserts it. */
  compensator: string | null;
}

export class ToolRegistry {
  private tools = new Map<string, ToolSpec>();

  register(spec: ToolSpec): void {
    if (this.tools.has(spec.name)) {
      throw new Error(`tool ${JSON.stringify(spec.name)} is already registered`);
    }
    this.tools.set(spec.name, spec);
  }

  get(name: string): ToolSpec {
    const spec = this.tools.get(name);
    if (spec === undefined) {
      throw new Error(`tool ${JSON.stringify(name)} is not registered`);
    }
    return spec;
  }

  /** Registered tool names in registration order (Map preserves insertion). */
  toolNames(): string[] {
    return [...this.tools.keys()];
  }

  has(name: string): boolean {
    return this.tools.has(name);
  }

  clear(): void {
    this.tools.clear();
  }
}

/** Global singleton — module-level state, exactly like Python's REGISTRY. */
export const REGISTRY = new ToolRegistry();

export interface ToolOptions {
  reversible?: boolean;
  name?: string;
  injectsHandle?: boolean;
  compensator?: string | null;
}

/** The wrapper an agent calls. Returns the tool result (reversible lane) or a
 *  StagedResult sentinel (irreversible lane), determined by the runtime. */
export type ToolWrapper<A extends Record<string, unknown>, R> = ((args: A) => R | StagedResultLike) & {
  toolSpec: ToolSpec;
};

// StagedResult lives in effects.ts; we only need its structural identity here
// for the return-type union, avoiding an import cycle at the type level.
interface StagedResultLike {
  readonly effectId: string;
}

/**
 * Register a tool and return its interception wrapper.
 *
 * The implementation `fn` receives an adapter-injected handle as its first
 * argument (for handle-injecting adapters like SQL) followed by the named-args
 * object, or just the named-args object when `injectsHandle` is false (HTTP).
 * The agent always calls the returned wrapper with a single named-args object;
 * that object is what the journal records.
 */
export function tool<A extends Record<string, unknown> = Record<string, unknown>, R = unknown>(
  resource: string,
  fn: ToolFn,
  options: ToolOptions = {},
): ToolWrapper<A, R> {
  const name = options.name ?? fn.name;
  if (!name) {
    throw new Error(
      "tool() could not derive a name: pass options.name for an anonymous function",
    );
  }
  const spec: ToolSpec = {
    name,
    fn,
    resource,
    reversible: options.reversible ?? true,
    injectsHandle: options.injectsHandle ?? true,
    compensator: options.compensator ?? null,
  };
  REGISTRY.register(spec);

  const wrapper = ((args: A): R | StagedResultLike => {
    const ctx = activeTxn.getStore();
    if (ctx === undefined) {
      // Outside agentTxn(): transparent passthrough, un-journalled.
      return fn(args) as R;
    }
    return ctx.recordToolCall(spec.name, args ?? {}) as R | StagedResultLike;
  }) as ToolWrapper<A, R>;
  wrapper.toolSpec = spec;
  return wrapper;
}
