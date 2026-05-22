/**
 * HttpAdapter — the irreversible adapter, honest about its inability to undo.
 * Mirror of pherix/core/adapters/http.py.
 *
 * `supportsRollback()` returns false, so the runtime forces every effect on
 * this adapter down the staged + gated lane: it never fires before commit, and
 * at commit it needs a registered compensator or an explicit human approval.
 * The snapshot/restore methods exist only to raise — there is no before-state
 * to capture, so calling them is a programming error.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

export class IrreversibleAdapterError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "IrreversibleAdapterError";
  }
}

export class HttpAdapter implements ResourceAdapter {
  readonly name = "http";

  supportsRollback(): boolean {
    return false;
  }

  snapshot(_effect: Effect): SnapshotHandle {
    throw new IrreversibleAdapterError(
      "HttpAdapter.snapshot() must not be called: irreversible effects are " +
        "staged at stage-time and fired at commit-time.",
    );
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // No handle injected — HTTP tools call their external client directly.
    return toolFn(effect.args);
  }

  restore(_handle: SnapshotHandle): void {
    throw new IrreversibleAdapterError(
      "HttpAdapter.restore() must not be called: there is no before-state. " +
        "Irreversible effects are unwound via compensator, not snapshot/restore.",
    );
  }
}
