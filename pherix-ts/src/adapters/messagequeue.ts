/**
 * MqAdapter — irreversible publish/pub-sub adapter + a publish tool harness.
 * Mirror of pherix/core/adapters/messagequeue.py.
 *
 * Publishing a message is irreversible in the same sense an outbound HTTP POST
 * is: once a broker accepts publish(topic, message) and fans it out to
 * subscribers, there is no before-image to restore — you cannot un-send it. So
 * `supportsRollback() -> false`, and the effect is staged (deferred to commit())
 * rather than fired live. The honest undo is a *compensator*: a second publish
 * of a tombstone / cancellation message, or a broker-side delete if the broker
 * exposes one. That is a semantic inverse, not a state rollback.
 *
 * The adapter is HttpAdapter-shaped. The value over a bare adapter is the
 * harness: `publishTool` registers a publish tool against an *injectable*
 * broker (any duck-typed object exposing publish(topic, message)), and
 * `tombstoneCompensator` registers the matching cancellation publish so a
 * rolled-back publish is followed by a tombstone on the same topic. Both are
 * testable against a tiny in-memory fake broker — no real broker, no network.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";
import { IrreversibleAdapterError } from "./http.js";
import { tool, type ToolWrapper } from "../tools.js";

/** The minimal duck-typed broker contract the harness needs. Any object with a
 *  publish(topic, message) method satisfies it — a real Kafka/RabbitMQ/SNS
 *  client wrapper, or the in-memory fake the tests use. May be sync or async. */
export interface Broker {
  publish(topic: string, message: unknown): unknown;
}

export class MqAdapter implements ResourceAdapter {
  readonly name = "mq";

  supportsRollback(): boolean {
    return false;
  }

  snapshot(_effect: Effect): SnapshotHandle {
    throw new IrreversibleAdapterError(
      "MqAdapter.snapshot() must not be called: a published message has no " +
        "before-image. Publishes are staged at stage-time and fired at " +
        "commit-time; the runtime must never request a snapshot here.",
    );
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // No handle injected — publish tools declare injectsHandle: false and own
    // the broker call (via their bound broker). The adapter passes the
    // journalled args object straight through.
    return toolFn(effect.args);
  }

  restore(_handle: SnapshotHandle): void {
    throw new IrreversibleAdapterError(
      "MqAdapter.restore() must not be called: a sent message cannot be " +
        "un-sent. Unwind a fired publish via a compensator (a tombstone / " +
        "cancellation publish), not via snapshot/restore.",
    );
  }
}

// --- the harness ---

export interface PublishToolOptions {
  broker: Broker;
  compensator?: string | null;
  resource?: string;
}

/**
 * Register and return an irreversible publish tool.
 *
 * The agent calls the tool with {topic, message}; both are journalled as the
 * effect's args. On rollback after the publish fired, the runtime invokes
 * `compensator` with those *same* args, so the paired compensator sees the
 * original topic / message and can publish a tombstone keyed on them.
 *
 * `broker` is injectable — a real client wrapper or a fake. `compensator` is
 * the name of a registered tool that semantically cancels this publish; Pherix
 * asserts its presence at stage-time and fires it on rollback.
 */
export function publishTool(
  name: string,
  options: PublishToolOptions,
): ToolWrapper<{ topic: string; message: unknown }, unknown> {
  const { broker, compensator = null, resource = "mq" } = options;
  return tool<{ topic: string; message: unknown }>(
    resource,
    (args: { topic: string; message: unknown }) => broker.publish(args.topic, args.message),
    { reversible: false, injectsHandle: false, name, compensator },
  );
}

export interface TombstoneCompensatorOptions {
  broker: Broker;
  /** Maps the original message to its cancellation payload. Defaults to
   *  wrapping as {tombstone: <message>}. */
  tombstone?: (message: unknown) => unknown;
  resource?: string;
}

/**
 * Register and return a compensator that cancels a prior publish.
 *
 * Pair this with `publishTool` by passing compensator: name. On rollback the
 * runtime calls it with the original publish's {topic, message}; it publishes a
 * cancellation onto the *same topic*. `tombstone` maps the original message to
 * its cancellation payload — by default it wraps the original as {tombstone:
 * <message>} so a subscriber can recognise and ignore the prior message. A
 * broker that supports true deletion can pass a `tombstone` returning a
 * delete-marker the broker honours.
 *
 * This is a *semantic left-inverse*, not a state restore: it does not undo the
 * fact that the original message was delivered — it publishes the opposite
 * action so downstream state converges back. That is the honest best a pub/sub
 * system can offer.
 */
export function tombstoneCompensator(
  name: string,
  options: TombstoneCompensatorOptions,
): ToolWrapper<{ topic: string; message: unknown }, unknown> {
  const { broker, tombstone, resource = "mq" } = options;
  const makeTombstone = tombstone ?? ((m: unknown) => ({ tombstone: m }));
  return tool<{ topic: string; message: unknown }>(
    resource,
    (args: { topic: string; message: unknown }) => broker.publish(args.topic, makeTombstone(args.message)),
    { reversible: false, injectsHandle: false, name },
  );
}
