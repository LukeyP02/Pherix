/**
 * RestAdapter — irreversible transport adapter + a REST/GraphQL tool harness.
 * Mirror of pherix/core/adapters/rest.py.
 *
 * The adapter itself is HttpAdapter-shaped: an outbound REST call (or a GraphQL
 * mutation, which is just a POST) has no before-image, so it cannot be
 * snapshot-restored. `supportsRollback() -> false` routes its effects down the
 * staging lane — the call does not fire at stage-time, it is deferred to
 * commit() and undone (if at all) via a registered compensator.
 *
 * The value over a bare HttpAdapter is the harness: `restTool` and
 * `graphqlTool` turn a SaaS endpoint into a Pherix tool in one call. They build
 * and register a `tool()`-wrapped function (reversible: false, injectsHandle:
 * false) and optionally pair it with a compensator name. The real network call
 * sits behind an *injectable* transport callable `transport(method, url, opts)`
 * so the harness is testable with no network. If no transport is given, one is
 * lazy-built over `fetch` at fire-time — the kernel never imports an HTTP
 * client at module top (Node 18+ has global `fetch`).
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";
import { IrreversibleAdapterError } from "./http.js";
import { tool, type ToolWrapper } from "../tools.js";

/** A transport is any callable with the shape fetch-wrappers already have:
 *  transport(method, url, opts) -> response-like. Kept duck-typed so a real
 *  client, a thin fetch shim, or a test fake all satisfy it without a
 *  structural dependency. */
export type Transport = (method: string, url: string, opts: Record<string, unknown>) => unknown;

export class RestAdapter implements ResourceAdapter {
  readonly name = "rest";

  supportsRollback(): boolean {
    return false;
  }

  snapshot(_effect: Effect): SnapshotHandle {
    throw new IrreversibleAdapterError(
      "RestAdapter.snapshot() must not be called: a REST/GraphQL call has no " +
        "before-image. Irreversible effects are staged at stage-time and fired " +
        "at commit-time; the runtime must never request a snapshot here.",
    );
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // No handle injected — REST tools declare injectsHandle: false and own the
    // call themselves (via their bound transport). The adapter passes the
    // journalled args object straight through.
    return toolFn(effect.args);
  }

  restore(_handle: SnapshotHandle): void {
    throw new IrreversibleAdapterError(
      "RestAdapter.restore() must not be called: there is no before-state to " +
        "restore. Unwind a fired REST effect via its compensator.",
    );
  }
}

// --- the harness ---

/** Build a fetch-based transport, lazily, so no HTTP client is referenced at
 *  module top. Returns a callable transport(method, url, opts). Recognised opts:
 *  `json` (a value, sent as a JSON body with the matching content-type), `data`
 *  (raw string/bytes body), `headers` (a record). The response is a small
 *  object {status, headers, body} with `body` JSON-decoded when possible. */
function defaultTransport(): Transport {
  return async (method: string, url: string, opts: Record<string, unknown>): Promise<unknown> => {
    const headers: Record<string, string> = { ...((opts["headers"] as Record<string, string>) ?? {}) };
    let body: string | undefined;
    if (opts["json"] !== undefined && opts["json"] !== null) {
      body = JSON.stringify(opts["json"]);
      if (!("Content-Type" in headers)) headers["Content-Type"] = "application/json";
    } else if (opts["data"] !== undefined && opts["data"] !== null) {
      body = opts["data"] as string;
    }
    const resp = await fetch(url, { method: method.toUpperCase(), headers, body });
    const raw = await resp.text();
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = raw;
    }
    return { status: resp.status, headers: Object.fromEntries(resp.headers.entries()), body: parsed };
  };
}

export interface RestToolOptions {
  method: string;
  url: string;
  transport?: Transport;
  compensator?: string | null;
  resource?: string;
}

/**
 * Register and return an irreversible REST tool.
 *
 * The returned tool's public signature is a single args object — any keys the
 * agent passes (`json`, `data`, `headers`, query params folded into `url`
 * upstream) are forwarded verbatim to the transport. The journalled `args` are
 * exactly that object, which is also what the compensator receives (the runtime
 * passes the original effect's args to the compensator), so a compensator
 * paired here sees the same payload the original send did.
 *
 * `transport` is injectable for testing; if omitted, a fetch transport is
 * lazy-built at fire-time. `compensator` is the name of another registered tool
 * that is this call's semantic inverse. Pherix resolves the name at fire-time
 * and asserts its presence at stage-time; it does not verify the inverse.
 */
export function restTool(name: string, options: RestToolOptions): ToolWrapper<Record<string, unknown>, unknown> {
  const { method, url, transport, compensator = null, resource = "rest" } = options;
  return tool<Record<string, unknown>>(
    resource,
    (args: Record<string, unknown>) => {
      const active = transport ?? defaultTransport();
      return active(method, url, args);
    },
    { reversible: false, injectsHandle: false, name, compensator },
  );
}

export interface GraphqlToolOptions {
  url: string;
  query: string;
  transport?: Transport;
  compensator?: string | null;
  resource?: string;
}

/**
 * Register and return an irreversible GraphQL tool.
 *
 * A GraphQL operation is just a POST of {query, variables} to a single
 * endpoint, so this is `restTool` with the body shape fixed. The `query` is
 * bound at registration; the agent supplies `variables` (an object) at
 * call-time. A GraphQL *mutation* is irreversible in exactly the way a REST
 * POST is — pair a compensating mutation via `compensator` to undo it.
 */
export function graphqlTool(name: string, options: GraphqlToolOptions): ToolWrapper<{ variables?: Record<string, unknown> }, unknown> {
  const { url, query, transport, compensator = null, resource = "rest" } = options;
  return tool<{ variables?: Record<string, unknown> }>(
    resource,
    (args: { variables?: Record<string, unknown> }) => {
      const active = transport ?? defaultTransport();
      return active("POST", url, { json: { query, variables: args?.variables ?? {} } });
    },
    { reversible: false, injectsHandle: false, name, compensator },
  );
}
