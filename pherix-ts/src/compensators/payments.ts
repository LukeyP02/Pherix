/**
 * Payment compensators — the highest-stakes inverses in the catalog.
 * Mirror of pherix/compensators/payments.py.
 *
 *   charge  → refund            (capture funds → return them)
 *   payout  → reverse_payout    (send funds out → claw them back)
 *
 * Both reverse by the idempotency key the caller supplies to the action —
 * exactly how real payment APIs (Stripe, Adyen, …) want it. Per the engine
 * contract the compensator only ever sees the action's *args*, never its
 * return value, so reversing off a shared key carried in the args is the
 * load-bearing pattern.
 */

import { tool, type ToolWrapper } from "../tools.js";

export interface PaymentsClient {
  charge(idempotencyKey: string, amountCents: number, currency: string): unknown;
  refund(idempotencyKey: string): unknown;
  payout(payoutId: string, amountCents: number, destination: string): unknown;
  reversePayout(payoutId: string): unknown;
}

export interface ChargeArgs extends Record<string, unknown> {
  idempotencyKey: string;
  amountCents: number;
  currency?: string;
}

/** Register `charge` and its left-inverse `refund`.
 *
 *  `charge` declares `refund` as its compensator, so a charge that has fired is
 *  auto-undone on rollback — no human approval needed. */
export function registerChargeRefund(
  client: PaymentsClient,
  resource = "payments",
): { charge: ToolWrapper<ChargeArgs, unknown>; refund: ToolWrapper<ChargeArgs, unknown> } {
  const refund = tool<ChargeArgs>(
    resource,
    // Reverses by the idempotency key alone; amountCents / currency are present
    // only because the runtime fires the compensator with the action's full
    // arg set (full-refund semantics).
    (args: ChargeArgs) => client.refund(args.idempotencyKey),
    { name: "refund", reversible: false, injectsHandle: false },
  );

  const charge = tool<ChargeArgs>(
    resource,
    (args: ChargeArgs) =>
      client.charge(args.idempotencyKey, args.amountCents, args.currency ?? "usd"),
    { name: "charge", reversible: false, injectsHandle: false, compensator: "refund" },
  );

  return { charge, refund };
}

export interface PayoutArgs extends Record<string, unknown> {
  payoutId: string;
  amountCents: number;
  destination: string;
}

/** Register `payout` and its left-inverse `reversePayout`. A payout is funds
 *  leaving the platform; reversing claws them back. Reverses by `payoutId`. */
export function registerPayoutReverse(
  client: PaymentsClient,
  resource = "payments",
): { payout: ToolWrapper<PayoutArgs, unknown>; reversePayout: ToolWrapper<PayoutArgs, unknown> } {
  const reversePayout = tool<PayoutArgs>(
    resource,
    (args: PayoutArgs) => client.reversePayout(args.payoutId),
    { name: "reversePayout", reversible: false, injectsHandle: false },
  );

  const payout = tool<PayoutArgs>(
    resource,
    (args: PayoutArgs) => client.payout(args.payoutId, args.amountCents, args.destination),
    { name: "payout", reversible: false, injectsHandle: false, compensator: "reversePayout" },
  );

  return { payout, reversePayout };
}
