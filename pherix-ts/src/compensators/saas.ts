/**
 * SaaS-API compensators — the everyday agent tool calls.
 * Mirror of pherix/compensators/saas.py.
 *
 *   GitHub   createPr     → closePr          (open a PR → close it)
 *   GitHub   addLabel     → removeLabel       (label an issue → unlabel)
 *   Slack    postMessage  → deleteMessage     (post → delete)
 *   Stripe   createCustomer→ deleteCustomer    (create → delete)
 *   SendGrid addContact   → removeContact      (add to list → remove)
 *   Twilio   sendSms      → (GATE, no comp)    (cannot unsend an SMS)
 *   Jira     createIssue  → deleteIssue        (create → delete)
 *
 * Each clean pair reverses off a shared id in the args. `sendSms` has no honest
 * inverse (a delivered SMS is gone), so it gates exactly like `sendEmail`.
 */

import { tool, type ToolWrapper } from "../tools.js";

// --- GitHub ---------------------------------------------------------------

export interface GithubClient {
  createPr(repo: string, branch: string, title: string, body: string): unknown;
  closePr(repo: string, branch: string): unknown;
  addLabel(repo: string, issue: number, label: string): unknown;
  removeLabel(repo: string, issue: number, label: string): unknown;
}

export interface PrArgs extends Record<string, unknown> {
  repo: string;
  branch: string;
  title: string;
  body: string;
}

/** Register `createPr` and its left-inverse `closePr`. Closing (not merging) is
 *  the honest inverse of opening: an open PR has not yet changed the base
 *  branch, so closing it returns the repo to its pre-PR state. Reverses by
 *  `(repo, branch)`. */
export function registerGithubPr(
  client: GithubClient,
  resource = "github",
): { createPr: ToolWrapper<PrArgs, unknown>; closePr: ToolWrapper<PrArgs, unknown> } {
  const closePr = tool<PrArgs>(resource, (args: PrArgs) => client.closePr(args.repo, args.branch), {
    name: "closePr",
    reversible: false,
    injectsHandle: false,
  });

  const createPr = tool<PrArgs>(
    resource,
    (args: PrArgs) => client.createPr(args.repo, args.branch, args.title, args.body),
    { name: "createPr", reversible: false, injectsHandle: false, compensator: "closePr" },
  );

  return { createPr, closePr };
}

export interface LabelArgs extends Record<string, unknown> {
  repo: string;
  issue: number;
  label: string;
}

/** Register `addLabel` and its left-inverse `removeLabel`. Reverses by
 *  `(repo, issue, label)`. */
export function registerGithubLabel(
  client: GithubClient,
  resource = "github",
): { addLabel: ToolWrapper<LabelArgs, unknown>; removeLabel: ToolWrapper<LabelArgs, unknown> } {
  const removeLabel = tool<LabelArgs>(
    resource,
    (args: LabelArgs) => client.removeLabel(args.repo, args.issue, args.label),
    { name: "removeLabel", reversible: false, injectsHandle: false },
  );

  const addLabel = tool<LabelArgs>(
    resource,
    (args: LabelArgs) => client.addLabel(args.repo, args.issue, args.label),
    { name: "addLabel", reversible: false, injectsHandle: false, compensator: "removeLabel" },
  );

  return { addLabel, removeLabel };
}

// --- Slack ----------------------------------------------------------------

export interface SlackClient {
  postMessage(channel: string, ts: string, text: string): unknown;
  deleteMessage(channel: string, ts: string): unknown;
}

export interface MessageArgs extends Record<string, unknown> {
  channel: string;
  ts: string;
  text: string;
}

/** Register `postMessage` and its left-inverse `deleteMessage`. Reverses by
 *  `(channel, ts)` — the caller supplies the message timestamp as the
 *  idempotency key, matching Slack's own `chat.delete`. */
export function registerSlackMessage(
  client: SlackClient,
  resource = "slack",
): {
  postMessage: ToolWrapper<MessageArgs, unknown>;
  deleteMessage: ToolWrapper<MessageArgs, unknown>;
} {
  const deleteMessage = tool<MessageArgs>(
    resource,
    (args: MessageArgs) => client.deleteMessage(args.channel, args.ts),
    { name: "deleteMessage", reversible: false, injectsHandle: false },
  );

  const postMessage = tool<MessageArgs>(
    resource,
    (args: MessageArgs) => client.postMessage(args.channel, args.ts, args.text),
    { name: "postMessage", reversible: false, injectsHandle: false, compensator: "deleteMessage" },
  );

  return { postMessage, deleteMessage };
}

// --- Stripe ---------------------------------------------------------------

export interface StripeClient {
  createCustomer(customerId: string, email: string): unknown;
  deleteCustomer(customerId: string): unknown;
}

export interface CustomerArgs extends Record<string, unknown> {
  customerId: string;
  email: string;
}

/** Register `createCustomer` and its left-inverse `deleteCustomer`. Reverses by
 *  `customerId`. */
export function registerStripeCustomer(
  client: StripeClient,
  resource = "stripe",
): {
  createCustomer: ToolWrapper<CustomerArgs, unknown>;
  deleteCustomer: ToolWrapper<CustomerArgs, unknown>;
} {
  const deleteCustomer = tool<CustomerArgs>(
    resource,
    (args: CustomerArgs) => client.deleteCustomer(args.customerId),
    { name: "deleteCustomer", reversible: false, injectsHandle: false },
  );

  const createCustomer = tool<CustomerArgs>(
    resource,
    (args: CustomerArgs) => client.createCustomer(args.customerId, args.email),
    {
      name: "createCustomer",
      reversible: false,
      injectsHandle: false,
      compensator: "deleteCustomer",
    },
  );

  return { createCustomer, deleteCustomer };
}

// --- SendGrid -------------------------------------------------------------

export interface SendgridClient {
  addContact(listId: string, email: string): unknown;
  removeContact(listId: string, email: string): unknown;
}

export interface ContactArgs extends Record<string, unknown> {
  listId: string;
  email: string;
}

/** Register `addContact` and its left-inverse `removeContact`. Reverses by
 *  `(listId, email)`. Adding a contact to a list is cleanly reversible — note
 *  this is distinct from *sending* mail, which is not (see
 *  `registerSendEmailGate`). */
export function registerSendgridContact(
  client: SendgridClient,
  resource = "sendgrid",
): { addContact: ToolWrapper<ContactArgs, unknown>; removeContact: ToolWrapper<ContactArgs, unknown> } {
  const removeContact = tool<ContactArgs>(
    resource,
    (args: ContactArgs) => client.removeContact(args.listId, args.email),
    { name: "removeContact", reversible: false, injectsHandle: false },
  );

  const addContact = tool<ContactArgs>(
    resource,
    (args: ContactArgs) => client.addContact(args.listId, args.email),
    { name: "addContact", reversible: false, injectsHandle: false, compensator: "removeContact" },
  );

  return { addContact, removeContact };
}

// --- Twilio ---------------------------------------------------------------

export interface TwilioClient {
  sendSms(to: string, body: string): unknown;
}

export interface SmsArgs extends Record<string, unknown> {
  to: string;
  body: string;
}

/** Register `sendSms` with no compensator — it gates at commit. Like a
 *  delivered email, a delivered SMS has no honest inverse. Returns the action
 *  only. */
export function registerTwilioSmsGate(
  client: TwilioClient,
  resource = "twilio",
): ToolWrapper<SmsArgs, unknown> {
  return tool<SmsArgs>(resource, (args: SmsArgs) => client.sendSms(args.to, args.body), {
    name: "sendSms",
    reversible: false,
    injectsHandle: false,
  });
}

// --- Jira -----------------------------------------------------------------

export interface JiraClient {
  createIssue(issueKey: string, project: string, summary: string): unknown;
  deleteIssue(issueKey: string): unknown;
}

export interface IssueArgs extends Record<string, unknown> {
  issueKey: string;
  project: string;
  summary: string;
}

/** Register `createIssue` and its left-inverse `deleteIssue`. Reverses by
 *  `issueKey`. */
export function registerJiraCreateDeleteIssue(
  client: JiraClient,
  resource = "jira",
): { createIssue: ToolWrapper<IssueArgs, unknown>; deleteIssue: ToolWrapper<IssueArgs, unknown> } {
  const deleteIssue = tool<IssueArgs>(
    resource,
    (args: IssueArgs) => client.deleteIssue(args.issueKey),
    { name: "deleteIssue", reversible: false, injectsHandle: false },
  );

  const createIssue = tool<IssueArgs>(
    resource,
    (args: IssueArgs) => client.createIssue(args.issueKey, args.project, args.summary),
    { name: "createIssue", reversible: false, injectsHandle: false, compensator: "deleteIssue" },
  );

  return { createIssue, deleteIssue };
}
