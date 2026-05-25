/**
 * Identity & comms compensators. Mirror of pherix/compensators/identity.py.
 *
 *   invite      → revokeInvite      (send an org/team invite → rescind it)
 *   grantRole   → revokeRole        (grant a permission → take it back)
 *   sendEmail   → (GATE, no comp)   (you cannot unsend an email — honest gate)
 *
 * The first two are clean left-inverses keyed by a shared id. The third is the
 * catalog's worked example of honesty about what cannot be undone: an email,
 * once delivered, is in the recipient's inbox forever. There is no semantic
 * inverse, so the action is registered with no compensator and `commit()` gates
 * on it — a human must call `approveIrreversible()`. The "undo" is the gate
 * itself: the chance to refuse *before* the irreversible thing happens.
 */

import { tool, type ToolWrapper } from "../tools.js";

export interface IdentityClient {
  invite(inviteId: string, email: string, org: string): unknown;
  revokeInvite(inviteId: string): unknown;
  grantRole(principal: string, role: string): unknown;
  revokeRole(principal: string, role: string): unknown;
  sendEmail(to: string, subject: string, body: string): unknown;
}

export interface InviteArgs extends Record<string, unknown> {
  inviteId: string;
  email: string;
  org: string;
}

/** Register `invite` and its left-inverse `revokeInvite`. Reverses by
 *  `inviteId` — the caller mints it, so action and revocation share the key. */
export function registerInviteRevoke(
  client: IdentityClient,
  resource = "identity",
): { invite: ToolWrapper<InviteArgs, unknown>; revokeInvite: ToolWrapper<InviteArgs, unknown> } {
  const revokeInvite = tool<InviteArgs>(
    resource,
    (args: InviteArgs) => client.revokeInvite(args.inviteId),
    { name: "revokeInvite", reversible: false, injectsHandle: false },
  );

  const invite = tool<InviteArgs>(
    resource,
    (args: InviteArgs) => client.invite(args.inviteId, args.email, args.org),
    { name: "invite", reversible: false, injectsHandle: false, compensator: "revokeInvite" },
  );

  return { invite, revokeInvite };
}

export interface RoleArgs extends Record<string, unknown> {
  principal: string;
  role: string;
}

/** Register `grantRole` and its left-inverse `revokeRole`. Reverses by
 *  `(principal, role)` — both carried in the args. */
export function registerGrantRevokeRole(
  client: IdentityClient,
  resource = "identity",
): { grantRole: ToolWrapper<RoleArgs, unknown>; revokeRole: ToolWrapper<RoleArgs, unknown> } {
  const revokeRole = tool<RoleArgs>(
    resource,
    (args: RoleArgs) => client.revokeRole(args.principal, args.role),
    { name: "revokeRole", reversible: false, injectsHandle: false },
  );

  const grantRole = tool<RoleArgs>(
    resource,
    (args: RoleArgs) => client.grantRole(args.principal, args.role),
    { name: "grantRole", reversible: false, injectsHandle: false, compensator: "revokeRole" },
  );

  return { grantRole, revokeRole };
}

export interface EmailArgs extends Record<string, unknown> {
  to: string;
  subject: string;
  body: string;
}

/** Register `sendEmail` with no compensator — it gates at commit. There is no
 *  honest left-inverse of a delivered email, so Pherix does not pretend one
 *  exists: the action stages and `commit()` blocks until a human calls
 *  `approveIrreversible(effectId)`. Returns the action only. */
export function registerSendEmailGate(
  client: IdentityClient,
  resource = "identity",
): ToolWrapper<EmailArgs, unknown> {
  return tool<EmailArgs>(
    resource,
    (args: EmailArgs) => client.sendEmail(args.to, args.subject, args.body),
    { name: "sendEmail", reversible: false, injectsHandle: false },
  );
}
