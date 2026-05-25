/**
 * Effect: one journalled tool call — the TypeScript mirror of pherix/core/effects.py.
 *
 * An Effect is a single entry in a Transaction's append-only effect journal.
 * `readKeys` / `writeKeys` slots exist from day one (isolation) but carry no
 * logic in the base SDK. `compensator` names a registered tool that is this
 * effect's semantic left-inverse: when an irreversible effect fails mid-commit,
 * the runtime invokes the compensator to undo it. Pherix does not verify the
 * inverse property — the developer asserts it.
 *
 * Effect args must be deterministically serialisable so the idempotency key
 * (`effectId`) is stable across runs and the audit journal can faithfully
 * persist them. Supported in args: anything natively JSON-serialisable, plus
 * `Uint8Array`/`Buffer` (base64), `Date` (ISO 8601). Anything else (functions,
 * symbols, `undefined`, circular refs, class instances) raises
 * `EffectArgsError` at Effect construction — silent coercion would let two
 * distinct non-serialisable objects collide on the same effectId, exactly the
 * bug we don't want at the idempotency boundary.
 */

import { createHash } from "node:crypto";

export enum EffectStatus {
  STAGED = "staged",
  APPLIED = "applied",
  COMPENSATED = "compensated",
  GATED = "gated",
  FAILED = "failed",
}

/** Raised when Effect args contain a value Pherix cannot journal. */
export class EffectArgsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EffectArgsError";
  }
}

/** A (resource, key, version) triple recorded for isolation. */
export type ReadKey = [resource: string, key: unknown, version: unknown];
/** A (resource, key, versionAfter) triple recorded for isolation. */
export type WriteKey = [resource: string, key: unknown, versionAfter: unknown];

/**
 * Sentinel returned to the agent when a staged irreversible tool is called.
 *
 * The agent receives the deterministic `effectId` so it can carry the
 * reference around (e.g. pass it to `approveIrreversible`), but the real
 * return value of the underlying tool only exists *after* commit fires the
 * effect. The agent therefore cannot branch on the result within the same
 * transaction — that is the partial-order property as a constraint on agent
 * code.
 */
export class StagedResult {
  constructor(public readonly effectId: string) {}
  toString(): string {
    return `StagedResult(effectId=${JSON.stringify(this.effectId)})`;
  }
  /** Journal-able plain shape — the analogue of Python serialising the frozen
   *  StagedResult dataclass via asdict(). */
  toJSON(): { effectId: string } {
    return { effectId: this.effectId };
  }
}

/**
 * Canonical, deterministic JSON of a value with object keys sorted, mirroring
 * Python's `json.dumps(..., sort_keys=True, default=strict_json_default)`.
 * Throws `EffectArgsError` (via the caller) on any non-journal-able value.
 */
export function canonicalJson(value: unknown, seen: WeakSet<object> = new WeakSet()): string {
  if (value === null) return "null";
  const t = typeof value;
  if (t === "string") return JSON.stringify(value);
  if (t === "boolean") return value ? "true" : "false";
  if (t === "number") {
    if (!Number.isFinite(value as number)) {
      throw new TypeError(`cannot journal non-finite number ${String(value)}`);
    }
    return JSON.stringify(value);
  }
  if (t === "bigint") {
    // bigint has no JSON form in Python's model; reject rather than coerce.
    throw new TypeError("cannot journal bigint; convert to string at the call site");
  }
  if (t === "undefined" || t === "function" || t === "symbol") {
    throw new TypeError(`cannot journal ${t}`);
  }
  // Object-like from here.
  const obj = value as object;
  // bytes -> base64, content-addressed and deterministic (mirrors strict_json_default).
  if (obj instanceof Uint8Array) {
    return JSON.stringify(`<bytes:b64:${Buffer.from(obj).toString("base64")}>`);
  }
  if (obj instanceof Date) {
    return JSON.stringify(obj.toISOString());
  }
  if (Array.isArray(obj)) {
    if (seen.has(obj)) throw new TypeError("cannot journal circular structure");
    seen.add(obj);
    const parts = obj.map((v) => canonicalJson(v, seen));
    seen.delete(obj);
    return `[${parts.join(",")}]`;
  }
  // toJSON is the JS convention for "I know how to serialise myself" — the
  // analogue of Python's strict_json_default accepting any dataclass. Honour it
  // before rejecting non-plain objects so value types (e.g. StagedResult) and
  // user dataclass-equivalents journal deterministically.
  if (typeof (obj as { toJSON?: unknown }).toJSON === "function") {
    return canonicalJson((obj as { toJSON(): unknown }).toJSON(), seen);
  }
  // Plain objects only. A class instance (non-plain prototype) is rejected,
  // the analogue of Python rejecting an arbitrary object: pass a plain shape.
  const proto = Object.getPrototypeOf(obj);
  if (proto !== Object.prototype && proto !== null) {
    throw new TypeError(
      `cannot journal value of type ${obj.constructor?.name ?? "unknown"}; ` +
        `convert to a plain object / supported type at the call site`,
    );
  }
  if (seen.has(obj)) throw new TypeError("cannot journal circular structure");
  seen.add(obj);
  const keys = Object.keys(obj as Record<string, unknown>).sort();
  const parts: string[] = [];
  for (const k of keys) {
    const v = (obj as Record<string, unknown>)[k];
    // Mirror json.dumps: keys whose value is `undefined` are simply absent in
    // JS object iteration anyway, but an explicit undefined is rejected above
    // only if reached as a value. Skip nothing here — every own key is encoded.
    parts.push(`${JSON.stringify(k)}:${canonicalJson(v, seen)}`);
  }
  seen.delete(obj);
  return `{${parts.join(",")}}`;
}

/**
 * Idempotency key = stable hash of (txnId, index, tool, sorted args).
 * Raises `EffectArgsError` if any arg is not deterministically serialisable.
 * Returns the first 16 hex chars of the sha256, matching the Python length.
 */
export function computeEffectId(
  txnId: string,
  index: number,
  tool: string,
  args: Record<string, unknown>,
): string {
  let payload: string;
  try {
    payload = canonicalJson({ args, index, tool, txn_id: txnId });
  } catch (exc) {
    throw new EffectArgsError(
      `tool ${JSON.stringify(tool)} got non-journal-able args: ${(exc as Error).message}. ` +
        `Effect args must be deterministically serialisable so the idempotency ` +
        `key (effectId) is stable across runs.`,
    );
  }
  return createHash("sha256").update(payload).digest("hex").slice(0, 16);
}

export interface SnapshotHandle {
  resource: string;
  effectIndex: number;
  payload: Record<string, unknown>;
}

export interface EffectInit {
  txnId: string;
  index: number;
  tool: string;
  args: Record<string, unknown>;
  resource: string;
  reversible: boolean;
  effectId?: string;
  compensator?: string | null;
}

/**
 * One journalled tool call. Field shapes mirror the Python `Effect` dataclass.
 * `effectId` is computed at construction when not supplied.
 */
export class Effect {
  txnId: string;
  index: number;
  tool: string;
  args: Record<string, unknown>;
  resource: string;
  reversible: boolean;
  effectId: string;
  readKeys: ReadKey[] = [];
  writeKeys: WriteKey[] = [];
  status: EffectStatus = EffectStatus.STAGED;
  snapshot: SnapshotHandle | null = null;
  result: unknown = null;
  compensator: string | null;
  ts: Date;

  constructor(init: EffectInit) {
    this.txnId = init.txnId;
    this.index = init.index;
    this.tool = init.tool;
    this.args = init.args;
    this.resource = init.resource;
    this.reversible = init.reversible;
    this.compensator = init.compensator ?? null;
    this.ts = new Date();
    this.effectId =
      init.effectId && init.effectId.length > 0
        ? init.effectId
        : computeEffectId(this.txnId, this.index, this.tool, this.args);
  }
}
