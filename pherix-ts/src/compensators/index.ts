/**
 * The vetted compensator catalog — common action/inverse pairs.
 * Mirror of pherix/compensators/__init__.py.
 *
 * A *compensator* is the semantic left-inverse of an irreversible tool:
 * `compensator ∘ tool ≈ identity`. Some side-effects cannot be snapshotted and
 * restored (charge a card, send an invite, create a cloud resource); the only
 * honest "undo" is to run an opposite action. This package ships a catalog of
 * those opposite actions, each tested as a true left-inverse.
 *
 * Every entry is a factory that takes a duck-typed client and registers the
 * action tool plus its compensator tool, returning the wrapped callables. Two
 * structural facts the factories are designed around:
 *
 *   1. The compensator receives the action's args, not its return value. On
 *      rollback the runtime builds a synthetic effect with `args=effect.args`
 *      and fires the compensator with those — so every pair reverses off a
 *      shared key carried in the args (the idempotency-key pattern).
 *   2. No compensator means the action gates. For genuinely un-undoable actions
 *      (you cannot unsend an email or an SMS) the factory registers the action
 *      with no compensator, so `commit()` blocks until a human calls
 *      `approveIrreversible()`. The honest undo is a human gate, not a fake
 *      inverse.
 *
 * The client is duck-typed: this package never imports a real SDK. The buyer
 * injects their real client; tests inject a fake in-memory one. The kernel
 * stays dependency-free.
 */

export {
  registerChargeRefund,
  registerPayoutReverse,
  type PaymentsClient,
  type ChargeArgs,
  type PayoutArgs,
} from "./payments.js";

export {
  registerInviteRevoke,
  registerGrantRevokeRole,
  registerSendEmailGate,
  type IdentityClient,
  type InviteArgs,
  type RoleArgs,
  type EmailArgs,
} from "./identity.js";

export {
  registerCreateDeleteResource,
  registerScaleUpDown,
  type ProvisioningClient,
  type ResourceArgs,
  type ScaleArgs,
} from "./provisioning.js";

export {
  registerGithubPr,
  registerGithubLabel,
  registerSlackMessage,
  registerStripeCustomer,
  registerSendgridContact,
  registerTwilioSmsGate,
  registerJiraCreateDeleteIssue,
  type GithubClient,
  type SlackClient,
  type StripeClient,
  type SendgridClient,
  type TwilioClient,
  type JiraClient,
  type PrArgs,
  type LabelArgs,
  type MessageArgs,
  type CustomerArgs,
  type ContactArgs,
  type SmsArgs,
  type IssueArgs,
} from "./saas.js";
