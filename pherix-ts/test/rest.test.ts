/**
 * RestAdapter — mirror of tests/test_adapters_rest.py.
 *
 * Irreversible adapter: the "left-inverse" property is replaced by the honest
 * one — supportsRollback() is false, snapshot/restore throw, and the adapter
 * forces the staged/gated lane. The partial-failure property is the
 * compensated-rollback path: a fired POST is undone by its registered DELETE
 * compensator when a LATER irreversible effect raises during the commit fold.
 *
 * All offline: the transport is an injectable fake that records calls and never
 * touches the network.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  EffectStatus,
  GateBlocked,
  IrreversibleAdapterError,
  REGISTRY,
  RestAdapter,
  StagedResult,
  agentTxn,
  graphqlTool,
  restTool,
  type Transport,
} from "../src/index.js";
import { Effect } from "../src/effects.js";

/** Records every (method, url, opts) and returns a canned response (or throws). */
class FakeTransport {
  calls: Array<{ method: string; url: string; opts: Record<string, unknown> }> = [];
  constructor(private readonly response: unknown = { status: 200 }, private readonly throws?: Error) {}
  fn: Transport = (method, url, opts) => {
    this.calls.push({ method, url, opts });
    if (this.throws !== undefined) throw this.throws;
    return this.response;
  };
}

beforeEach(() => {
  REGISTRY.clear();
});

describe("RestAdapter — honest irreversibility", () => {
  it("supportsRollback() is false", () => {
    expect(new RestAdapter().supportsRollback()).toBe(false);
    expect(new RestAdapter().name).toBe("rest");
  });

  it("snapshot() throws — there is no before-image", () => {
    const effect = new Effect({ txnId: "t", index: 0, tool: "x", args: {}, resource: "rest", reversible: false });
    expect(() => new RestAdapter().snapshot(effect)).toThrow(IrreversibleAdapterError);
  });

  it("restore() throws — there is no before-state", () => {
    expect(() => new RestAdapter().restore({ resource: "rest", effectIndex: 0, payload: {} })).toThrow(
      IrreversibleAdapterError,
    );
  });

  it("apply() invokes the tool with the journalled args, no handle injected", () => {
    const effect = new Effect({
      txnId: "t",
      index: 0,
      tool: "create_user",
      args: { json: { name: "ada" } },
      resource: "rest",
      reversible: false,
    });
    const seen: unknown[] = [];
    const result = new RestAdapter().apply(effect, (args: unknown) => {
      seen.push(args);
      return { status: 201 };
    });
    expect(seen).toEqual([{ json: { name: "ada" } }]);
    expect(result).toEqual({ status: 201 });
  });
});

describe("RestAdapter harness — restTool staging lane", () => {
  it("passes through to the transport outside a transaction", async () => {
    const t = new FakeTransport({ status: 201, body: { id: 1 } });
    const create = restTool("create_user", { method: "POST", url: "https://api/users", transport: t.fn });
    const out = await create({ json: { name: "ada" } });
    expect(out).toEqual({ status: 201, body: { id: 1 } });
    expect(t.calls).toEqual([{ method: "POST", url: "https://api/users", opts: { json: { name: "ada" } } }]);
  });

  it("does not fire at stage-time; fires exactly once at commit", async () => {
    const t = new FakeTransport({ status: 201 });
    const create = restTool("create_user", { method: "POST", url: "https://api/users", transport: t.fn });
    const ctx = await agentTxn({ rest: new RestAdapter() }, async (txn) => {
      const staged = (await create({ json: { name: "ada" } })) as StagedResult;
      expect(t.calls).toHaveLength(0); // staged, nothing fired yet
      txn.approveIrreversible(staged.effectId);
    });
    expect(t.calls).toHaveLength(1);
    expect(ctx.txn.effects[0]!.status).toBe(EffectStatus.APPLIED);
  });

  it("gates without a compensator or approval", async () => {
    const t = new FakeTransport();
    const create = restTool("create_user", { method: "POST", url: "https://api/users", transport: t.fn });
    await expect(
      agentTxn({ rest: new RestAdapter() }, async () => {
        await create({ json: { name: "ada" } });
      }),
    ).rejects.toThrow(GateBlocked);
    expect(t.calls).toHaveLength(0); // gate-block unwinds without firing
  });

  it("marks the effect FAILED when the transport raises at commit", async () => {
    const t = new FakeTransport(undefined, new Error("503 from SaaS"));
    const create = restTool("create_user", { method: "POST", url: "https://api/users", transport: t.fn });
    let ctx: Awaited<ReturnType<typeof agentTxn>> | undefined;
    await expect(
      agentTxn({ rest: new RestAdapter() }, async (txn) => {
        ctx = txn;
        const r = (await create({ json: { name: "ada" } })) as StagedResult;
        txn.approveIrreversible(r.effectId);
      }),
    ).rejects.toThrow("503 from SaaS");
    expect(t.calls).toHaveLength(1);
    expect(ctx!.txn.effects[0]!.status).toBe(EffectStatus.FAILED);
  });
});

describe("RestAdapter harness — compensated rollback (the partial-failure path)", () => {
  it("compensates a fired POST with its DELETE inverse on partial failure", async () => {
    const sends = new FakeTransport({ status: 201, body: { id: "u_1" } });
    const deletes = new FakeTransport({ status: 204 });
    restTool("delete_user", { method: "DELETE", url: "https://api/users/u_1", transport: deletes.fn });
    const create = restTool("create_user", {
      method: "POST",
      url: "https://api/users",
      transport: sends.fn,
      compensator: "delete_user",
    });
    const boom = restTool("send_welcome", {
      method: "POST",
      url: "https://api/email",
      transport: new FakeTransport(undefined, new Error("smtp down")).fn,
    });
    let ctx: Awaited<ReturnType<typeof agentTxn>> | undefined;
    await expect(
      agentTxn({ rest: new RestAdapter() }, async (txn) => {
        ctx = txn;
        await create({ json: { name: "ada" } });
        const r2 = (await boom({ json: { to: "ada@x.io" } })) as StagedResult;
        txn.approveIrreversible(r2.effectId);
      }),
    ).rejects.toThrow("smtp down");
    expect(sends.calls).toHaveLength(1);
    expect(deletes.calls).toHaveLength(1);
    expect(deletes.calls[0]!.method).toBe("DELETE");
    expect(ctx!.txn.effects[0]!.status).toBe(EffectStatus.COMPENSATED);
  });

  it("hands the compensator the original send's journalled args", async () => {
    const seenCompArgs: unknown[] = [];
    // The compensator is just another irreversible tool keyed by name.
    restTool("undo_create", {
      method: "DELETE",
      url: "https://api/users",
      transport: (_m, _u, opts) => {
        seenCompArgs.push(opts);
        return { status: 204 };
      },
    });
    const create = restTool("create_user", {
      method: "POST",
      url: "https://api/users",
      transport: new FakeTransport().fn,
      compensator: "undo_create",
    });
    const boom = restTool("send_welcome", {
      method: "POST",
      url: "https://api/email",
      transport: new FakeTransport(undefined, new Error("smtp down")).fn,
    });
    await expect(
      agentTxn({ rest: new RestAdapter() }, async (txn) => {
        await create({ json: { name: "ada" }, headers: { x: "1" } });
        const r2 = (await boom({ json: { to: "ada@x.io" } })) as StagedResult;
        txn.approveIrreversible(r2.effectId);
      }),
    ).rejects.toThrow("smtp down");
    // The compensator's transport saw the ORIGINAL send's args verbatim.
    expect(seenCompArgs).toEqual([{ json: { name: "ada" }, headers: { x: "1" } }]);
  });
});

describe("RestAdapter harness — graphqlTool", () => {
  it("posts {query, variables} and stages until commit", async () => {
    const t = new FakeTransport({ status: 200, body: { data: {} } });
    const mutation = "mutation($name:String!){ createUser(name:$name){ id } }";
    const run = graphqlTool("gql_create_user", { url: "https://api/graphql", query: mutation, transport: t.fn });
    await agentTxn({ rest: new RestAdapter() }, async (txn) => {
      const r = (await run({ variables: { name: "ada" } })) as StagedResult;
      expect(t.calls).toHaveLength(0); // staged, not fired
      txn.approveIrreversible(r.effectId);
    });
    expect(t.calls).toEqual([
      { method: "POST", url: "https://api/graphql", opts: { json: { query: mutation, variables: { name: "ada" } } } },
    ]);
  });

  it("defaults variables to an empty object", async () => {
    const t = new FakeTransport();
    const run = graphqlTool("gql_ping", { url: "https://api/graphql", query: "{ping}", transport: t.fn });
    await agentTxn({ rest: new RestAdapter() }, async (txn) => {
      const r = (await run({})) as StagedResult;
      txn.approveIrreversible(r.effectId);
    });
    expect((t.calls[0]!.opts["json"] as { variables: unknown }).variables).toEqual({});
  });

  it("compensates a mutation with a sibling mutation on partial failure", async () => {
    const fwd = new FakeTransport({ status: 200 });
    const inv = new FakeTransport({ status: 200 });
    graphqlTool("gql_uninvite", {
      url: "https://api/graphql",
      query: "mutation($e:String!){ revokeInvite(email:$e) }",
      transport: inv.fn,
    });
    const invite = graphqlTool("gql_invite", {
      url: "https://api/graphql",
      query: "mutation($e:String!){ invite(email:$e) }",
      transport: fwd.fn,
      compensator: "gql_uninvite",
    });
    const boom = graphqlTool("gql_boom", {
      url: "https://api/graphql",
      query: "mutation { willFail }",
      transport: new FakeTransport(undefined, new Error("graphql 500")).fn,
    });
    let ctx: Awaited<ReturnType<typeof agentTxn>> | undefined;
    await expect(
      agentTxn({ rest: new RestAdapter() }, async (txn) => {
        ctx = txn;
        await invite({ variables: { e: "ada@x.io" } });
        const r2 = (await boom({})) as StagedResult;
        txn.approveIrreversible(r2.effectId);
      }),
    ).rejects.toThrow("graphql 500");
    expect(fwd.calls).toHaveLength(1);
    expect(inv.calls).toHaveLength(1);
    expect(ctx!.txn.effects[0]!.status).toBe(EffectStatus.COMPENSATED);
  });
});
