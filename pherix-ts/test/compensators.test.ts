/** Mirrors tests/test_compensators_*.py — the vetted catalog as true
 *  left-inverses, including the partial-failure (mid-commit) unwind path. */

import { beforeEach, describe, expect, it } from "vitest";
import {
  GateBlocked,
  HttpAdapter,
  REGISTRY,
  StagedResult,
  TxnState,
  agentTxn,
  registerChargeRefund,
  registerInviteRevoke,
  registerScaleUpDown,
  registerSendEmailGate,
  tool,
  type IdentityClient,
  type PaymentsClient,
  type ProvisioningClient,
  type TxnContext,
} from "../src/index.js";

// A duck-typed fake covering every client surface the catalog touches.
class FakeWorld implements PaymentsClient, ProvisioningClient, IdentityClient {
  charges = new Map<string, number>();
  replicas = new Map<string, number>();
  invites = new Set<string>();
  sentEmails: string[] = [];

  charge(key: string, amount: number): unknown {
    this.charges.set(key, amount);
    return { id: key };
  }
  refund(key: string): unknown {
    this.charges.delete(key);
    return { refunded: key };
  }
  payout(): unknown {
    return {};
  }
  reversePayout(): unknown {
    return {};
  }
  createResource(): unknown {
    return {};
  }
  deleteResource(): unknown {
    return {};
  }
  scale(target: string, replicas: number): unknown {
    this.replicas.set(target, replicas);
    return { target, replicas };
  }
  invite(inviteId: string): unknown {
    this.invites.add(inviteId);
    return { id: inviteId };
  }
  revokeInvite(inviteId: string): unknown {
    this.invites.delete(inviteId);
    return { revoked: inviteId };
  }
  grantRole(): unknown {
    return {};
  }
  revokeRole(): unknown {
    return {};
  }
  sendEmail(to: string): unknown {
    this.sentEmails.push(to);
    return { delivered: to };
  }
}

let world: FakeWorld;
let adapters: Record<string, HttpAdapter>;

beforeEach(() => {
  REGISTRY.clear();
  world = new FakeWorld();
  const http = new HttpAdapter();
  // One irreversible adapter serves every resource key the catalog uses.
  adapters = { payments: http, provisioning: http, identity: http };
});

/** A staged irreversible that throws when fired — pre-approved so it clears the
 *  gate and reaches the fire loop, where its throw drives the mixed-fold
 *  unwind that exercises the already-fired effects' compensators. */
function exploder(resource: string) {
  return tool(
    resource,
    () => {
      throw new Error("boom at fire");
    },
    { name: "exploder", reversible: false, injectsHandle: false },
  );
}

describe("charge → refund (payments)", () => {
  it("clean commit fires the charge and never refunds", async () => {
    const { charge } = registerChargeRefund(world);
    const ctx = await agentTxn(adapters, async () => {
      await charge({ idempotencyKey: "k1", amountCents: 500 });
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(world.charges.get("k1")).toBe(500);
  });

  it("a mid-commit failure refunds the already-fired charge (left-inverse)", async () => {
    const { charge } = registerChargeRefund(world);
    const boom = exploder("payments");
    let captured: TxnContext | undefined;

    await expect(
      agentTxn(adapters, async (txn) => {
        captured = txn;
        await charge({ idempotencyKey: "k1", amountCents: 500 });
        const staged = (await boom({})) as StagedResult;
        txn.approveIrreversible(staged.effectId);
      }),
    ).rejects.toThrow("boom at fire");

    // charge fired then was compensated: refund(k1) ran, world is back to empty.
    expect(world.charges.size).toBe(0);
    expect(captured!.txn.state).toBe(TxnState.ROLLED_BACK);
  });
});

describe("scaleUp → scaleDown (before-value carried in args)", () => {
  it("a mid-commit failure restores the prior replica count", async () => {
    world.replicas.set("api", 2); // prior capacity
    const { scaleUp } = registerScaleUpDown(world);
    const boom = exploder("provisioning");

    await expect(
      agentTxn(adapters, async (txn) => {
        await scaleUp({ target: "api", fromReplicas: 2, toReplicas: 10 });
        const staged = (await boom({})) as StagedResult;
        txn.approveIrreversible(staged.effectId);
      }),
    ).rejects.toThrow("boom at fire");

    // scaleUp set api->10 at fire, scaleDown restored it to fromReplicas=2.
    expect(world.replicas.get("api")).toBe(2);
  });
});

describe("invite → revokeInvite (identity)", () => {
  it("a mid-commit failure revokes the issued invite", async () => {
    const { invite } = registerInviteRevoke(world);
    const boom = exploder("identity");

    await expect(
      agentTxn(adapters, async (txn) => {
        await invite({ inviteId: "inv-1", email: "a@b.c", org: "acme" });
        const staged = (await boom({})) as StagedResult;
        txn.approveIrreversible(staged.effectId);
      }),
    ).rejects.toThrow("boom at fire");

    expect(world.invites.has("inv-1")).toBe(false);
  });
});

describe("sendEmail gate (no honest inverse)", () => {
  it("gates at commit and never delivers without approval", async () => {
    const sendEmail = registerSendEmailGate(world);
    let captured: TxnContext | undefined;

    await expect(
      agentTxn(adapters, async (txn) => {
        captured = txn;
        await sendEmail({ to: "a@b.c", subject: "hi", body: "x" });
      }),
    ).rejects.toThrow(GateBlocked);

    expect(world.sentEmails).toHaveLength(0); // never fired — the gate held
    expect(captured!.txn.state).toBe(TxnState.ROLLED_BACK);
  });

  it("delivers once explicitly approved", async () => {
    const sendEmail = registerSendEmailGate(world);
    const ctx = await agentTxn(adapters, async (txn) => {
      const staged = (await sendEmail({ to: "a@b.c", subject: "hi", body: "x" })) as StagedResult;
      txn.approveIrreversible(staged.effectId);
    });
    expect(ctx.txn.state).toBe(TxnState.COMMITTED);
    expect(world.sentEmails).toEqual(["a@b.c"]);
  });
});
