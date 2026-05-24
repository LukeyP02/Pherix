/**
 * MqAdapter — mirror of tests/test_adapters_messagequeue.py.
 *
 * Irreversible adapter: the "left-inverse" property is the honest one —
 * supportsRollback() is false, snapshot/restore throw, the staged lane is
 * forced. The partial-failure property is the tombstone-compensated rollback: a
 * fired publish is followed by a cancellation publish on the same topic when a
 * LATER irreversible effect raises during the commit fold.
 *
 * All offline: an in-memory fake broker records published messages; no real
 * broker, no network.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  EffectStatus,
  GateBlocked,
  IrreversibleAdapterError,
  MqAdapter,
  REGISTRY,
  StagedResult,
  TxnState,
  agentTxn,
  publishTool,
  tombstoneCompensator,
  type Broker,
} from "../src/index.js";
import { Effect } from "../src/effects.js";

class FakeBroker implements Broker {
  published: Array<[string, unknown]> = [];
  constructor(private readonly throws?: Error) {}
  publish(topic: string, message: unknown): unknown {
    if (this.throws !== undefined) throw this.throws;
    this.published.push([topic, message]);
    return { acked: true, offset: this.published.length - 1 };
  }
}

beforeEach(() => {
  REGISTRY.clear();
});

describe("MqAdapter — honest irreversibility", () => {
  it("supportsRollback() is false", () => {
    expect(new MqAdapter().supportsRollback()).toBe(false);
    expect(new MqAdapter().name).toBe("mq");
  });

  it("snapshot() throws — a published message has no before-image", () => {
    const effect = new Effect({ txnId: "t", index: 0, tool: "x", args: {}, resource: "mq", reversible: false });
    expect(() => new MqAdapter().snapshot(effect)).toThrow(IrreversibleAdapterError);
  });

  it("restore() throws — a sent message cannot be un-sent", () => {
    expect(() => new MqAdapter().restore({ resource: "mq", effectIndex: 0, payload: {} })).toThrow(
      IrreversibleAdapterError,
    );
  });

  it("apply() invokes the tool with the journalled args, no handle injected", () => {
    const effect = new Effect({
      txnId: "t",
      index: 0,
      tool: "emit",
      args: { topic: "orders", message: { id: 1 } },
      resource: "mq",
      reversible: false,
    });
    const seen: unknown[] = [];
    const result = new MqAdapter().apply(effect, (args: { topic: string; message: unknown }) => {
      seen.push({ topic: args.topic, message: args.message });
      return { acked: true };
    });
    expect(seen).toEqual([{ topic: "orders", message: { id: 1 } }]);
    expect(result).toEqual({ acked: true });
  });
});

describe("MqAdapter harness — publishTool staging lane", () => {
  it("passes through to the broker outside a transaction", async () => {
    const broker = new FakeBroker();
    const emit = publishTool("emit_order", { broker });
    const out = (await emit({ topic: "orders", message: { id: 1 } })) as { acked: boolean };
    expect(out.acked).toBe(true);
    expect(broker.published).toEqual([["orders", { id: 1 }]]);
  });

  it("does not publish at stage-time; fires once at commit", async () => {
    const broker = new FakeBroker();
    const emit = publishTool("emit_order", { broker });
    const ctx = await agentTxn({ mq: new MqAdapter() }, async (txn) => {
      const r = (await emit({ topic: "orders", message: { id: 1 } })) as StagedResult;
      expect(broker.published).toHaveLength(0); // staged, not published
      txn.approveIrreversible(r.effectId);
    });
    expect(broker.published).toEqual([["orders", { id: 1 }]]);
    expect(ctx.txn.effects[0]!.status).toBe(EffectStatus.APPLIED);
  });

  it("rollback before commit never publishes", async () => {
    const broker = new FakeBroker();
    const emit = publishTool("emit_order", { broker });
    const ctx = await agentTxn({ mq: new MqAdapter() }, async (txn) => {
      await emit({ topic: "orders", message: { id: 1 } });
      await txn.rollback();
    });
    expect(broker.published).toHaveLength(0); // never sent
    expect(ctx.txn.effects[0]!.status).toBe(EffectStatus.STAGED);
  });

  it("gates without a compensator or approval", async () => {
    const broker = new FakeBroker();
    const emit = publishTool("emit_order", { broker });
    await expect(
      agentTxn({ mq: new MqAdapter() }, async () => {
        await emit({ topic: "orders", message: { id: 1 } });
      }),
    ).rejects.toThrow(GateBlocked);
    expect(broker.published).toHaveLength(0);
  });

  it("marks the effect FAILED when the broker raises at commit", async () => {
    const broker = new FakeBroker(new Error("broker unreachable"));
    const emit = publishTool("emit_order", { broker });
    let ctx: Awaited<ReturnType<typeof agentTxn>> | undefined;
    await expect(
      agentTxn({ mq: new MqAdapter() }, async (txn) => {
        ctx = txn;
        const r = (await emit({ topic: "orders", message: { id: 1 } })) as StagedResult;
        txn.approveIrreversible(r.effectId);
      }),
    ).rejects.toThrow("broker unreachable");
    expect(ctx!.txn.effects[0]!.status).toBe(EffectStatus.FAILED);
  });
});

describe("MqAdapter harness — tombstone compensator (the partial-failure path)", () => {
  /** A second irreversible publish that fails during the commit fold, so the
   *  runtime walks back and fires the earlier compensator-backed publish's
   *  compensator. */
  function failingPublish(name = "boom"): ReturnType<typeof publishTool> {
    return publishTool(name, { broker: new FakeBroker(new Error("broker down")) });
  }

  it("publishes the tombstone inverse on the same topic on partial failure", async () => {
    const broker = new FakeBroker();
    tombstoneCompensator("cancel_order", { broker });
    const emit = publishTool("emit_order", { broker, compensator: "cancel_order" });
    const boom = failingPublish();
    let ctx: Awaited<ReturnType<typeof agentTxn>> | undefined;
    await expect(
      agentTxn({ mq: new MqAdapter() }, async (txn) => {
        ctx = txn;
        await emit({ topic: "orders", message: { id: 1 } });
        const r2 = (await boom({ topic: "x", message: {} })) as StagedResult;
        txn.approveIrreversible(r2.effectId);
      }),
    ).rejects.toThrow("broker down");
    expect(broker.published).toEqual([
      ["orders", { id: 1 }],
      ["orders", { tombstone: { id: 1 } }],
    ]);
    expect(ctx!.txn.effects[0]!.status).toBe(EffectStatus.COMPENSATED);
    expect(ctx!.txn.state).toBe(TxnState.ROLLED_BACK);
  });

  it("honours a custom tombstone mapping (broker-side delete-marker)", async () => {
    const broker = new FakeBroker();
    tombstoneCompensator("cancel_order", {
      broker,
      tombstone: (m: unknown) => ({ op: "delete", key: (m as { id: number }).id }),
    });
    const emit = publishTool("emit_order", { broker, compensator: "cancel_order" });
    const boom = failingPublish();
    await expect(
      agentTxn({ mq: new MqAdapter() }, async (txn) => {
        await emit({ topic: "orders", message: { id: 7 } });
        const r2 = (await boom({ topic: "x", message: {} })) as StagedResult;
        txn.approveIrreversible(r2.effectId);
      }),
    ).rejects.toThrow("broker down");
    expect(broker.published[1]).toEqual(["orders", { op: "delete", key: 7 }]);
  });

  it("hands the compensator the original topic and message", async () => {
    const seen: unknown[] = [];
    const sink = new FakeBroker();
    tombstoneCompensator("cancel", {
      broker: sink,
      tombstone: (message: unknown) => {
        seen.push(message);
        return { tombstone: message };
      },
    });
    const emit = publishTool("emit", { broker: sink, compensator: "cancel" });
    const boom = failingPublish();
    await expect(
      agentTxn({ mq: new MqAdapter() }, async (txn) => {
        await emit({ topic: "billing", message: { amount: 50 } });
        const r2 = (await boom({ topic: "x", message: {} })) as StagedResult;
        txn.approveIrreversible(r2.effectId);
      }),
    ).rejects.toThrow("broker down");
    expect(seen).toEqual([{ amount: 50 }]);
    expect(sink.published[1]![0]).toBe("billing");
  });
});
