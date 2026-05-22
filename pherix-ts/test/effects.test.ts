/** Mirrors tests/test_effects.py — effectId determinism + serialisation. */

import { describe, expect, it } from "vitest";
import { Effect, EffectArgsError, EffectStatus, StagedResult, computeEffectId } from "../src/index.js";

function mkEffect(args: Record<string, unknown>, opts: Partial<{ index: number; tool: string; txnId: string }> = {}): Effect {
  return new Effect({
    txnId: opts.txnId ?? "txn-1",
    index: opts.index ?? 0,
    tool: opts.tool ?? "t",
    args,
    resource: "sql",
    reversible: true,
  });
}

describe("effectId", () => {
  it("is derived deterministically when not supplied", () => {
    const a = mkEffect({ x: 1 });
    const b = mkEffect({ x: 1 });
    expect(a.effectId).toBe(b.effectId);
    expect(a.effectId).toHaveLength(16);
  });

  it("is independent of arg key order", () => {
    const a = computeEffectId("txn-1", 0, "t", { a: 1, b: 2 });
    const b = computeEffectId("txn-1", 0, "t", { b: 2, a: 1 });
    expect(a).toBe(b);
  });

  it("varies with index, tool, and txnId", () => {
    const base = mkEffect({ x: 1 }).effectId;
    expect(mkEffect({ x: 1 }, { index: 1 }).effectId).not.toBe(base);
    expect(mkEffect({ x: 1 }, { tool: "u" }).effectId).not.toBe(base);
    expect(mkEffect({ x: 1 }, { txnId: "txn-2" }).effectId).not.toBe(base);
  });

  it("preserves an explicitly supplied effectId", () => {
    const e = new Effect({
      txnId: "txn-1",
      index: 0,
      tool: "t",
      args: {},
      resource: "sql",
      reversible: true,
      effectId: "deadbeefdeadbeef",
    });
    expect(e.effectId).toBe("deadbeefdeadbeef");
  });

  it("throws EffectArgsError on non-serialisable args (function)", () => {
    expect(() => mkEffect({ cb: () => 1 })).toThrow(EffectArgsError);
  });

  it("throws on a class instance arg", () => {
    class Custom {
      v = 1;
    }
    expect(() => mkEffect({ obj: new Custom() })).toThrow(EffectArgsError);
  });

  it("handles bytes and Date args deterministically", () => {
    const bytes = new Uint8Array([1, 2, 3]);
    const d = new Date("2024-01-01T00:00:00.000Z");
    const a = computeEffectId("txn-1", 0, "t", { b: bytes, d });
    const b = computeEffectId("txn-1", 0, "t", { b: new Uint8Array([1, 2, 3]), d: new Date("2024-01-01T00:00:00.000Z") });
    expect(a).toBe(b);
  });
});

describe("StagedResult", () => {
  it("carries the effectId", () => {
    const s = new StagedResult("abc123");
    expect(s.effectId).toBe("abc123");
  });
});

describe("Effect defaults", () => {
  it("starts STAGED with no snapshot and empty key sets", () => {
    const e = mkEffect({ x: 1 });
    expect(e.status).toBe(EffectStatus.STAGED);
    expect(e.snapshot).toBeNull();
    expect(e.readKeys).toEqual([]);
    expect(e.writeKeys).toEqual([]);
  });
});
