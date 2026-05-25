/**
 * RedisAdapter — snapshot-the-touched-key reversibility over a key-value store.
 * Mirror of pherix/core/adapters/redis.py.
 *
 * Redis has no per-command savepoint: a `SET` / `DEL` / `HSET` lands
 * immediately. But the *value* under a touched key is small enough to copy. So
 * reversibility is again the filesystem-adapter machinery — capture the
 * **before-image** of each touched key in `snapshot`, write it back in
 * `restore`.
 *
 * Why DUMP/RESTORE, not GET/SET. `GET` only works on string keys; an effect
 * might touch a hash, list, set, or sorted set. `DUMP` returns the
 * Redis-internal serialisation of *any* value type, and `RESTORE` rebuilds it
 * exactly — preserving the value's type. We pair it with `PTTL` so the key's
 * expiry is restored too. The before-image per key is therefore
 * `{existed: true, dump: <b64>, pttl: <ms or -1>}` or `{existed: false}`.
 *
 * Touched-keys convention (route b). The adapter learns the touched key(s) from
 * `effect.args` by name: `args.key` (single) and/or `args.keys` (list).
 *
 * Atomicity (honest). A multi-key effect is not atomic at the Redis layer; the
 * journal backward-fold is what makes the *effect* atomic — `restore` rewrites
 * every captured key back regardless of how far `apply` got. `restore` itself
 * runs inside one MULTI/EXEC pipeline so the *undo* of one effect is one
 * round-trip and is not interleaved with another client's commands.
 *
 * The driver is kept structural (`RedisClient` / `RedisPipeline`) so this
 * module forces no `redis`/`ioredis` types on consumers and tests can
 * substitute an in-memory fake. The client is constructed by the caller.
 */

import type { Effect, SnapshotHandle } from "../effects.js";
import type { ResourceAdapter, ToolFn } from "./base.js";

/** A MULTI/EXEC pipeline: queued writes, applied atomically on `exec`. */
export interface RedisPipeline {
  del(key: string): unknown;
  restore(key: string, ttlMs: number, serialized: Uint8Array): unknown;
  exec(): Promise<unknown> | unknown;
}

/** The slice of a redis-client surface this adapter uses. `dump` returns the
 *  raw serialisation bytes or null (absent key); `pttl` the millisecond TTL
 *  (-1 no expiry, -2 missing). `pipeline()` opens a MULTI/EXEC transaction. */
export interface RedisClient {
  dump(key: string): Promise<Uint8Array | null> | Uint8Array | null;
  pttl(key: string): Promise<number> | number;
  pipeline(): RedisPipeline;
}

interface KeyRecord {
  existed: boolean;
  dump: string | null; // base64 of DUMP bytes
  pttl: number;
}

export class RedisAdapter implements ResourceAdapter {
  readonly name = "redis";
  private readonly redis: RedisClient;

  constructor(client: RedisClient) {
    this.redis = client;
  }

  get client(): RedisClient {
    return this.redis;
  }

  supportsRollback(): boolean {
    return true;
  }

  // --- touched-key extraction --------------------------------------------

  private static touchedKeys(effect: Effect): string[] {
    const keys: string[] = [];
    const single = effect.args["key"];
    if (single !== undefined && single !== null) keys.push(single as string);
    const multi = effect.args["keys"];
    if (multi) keys.push(...(multi as string[]));
    const seen = new Set<string>();
    const out: string[] = [];
    for (const k of keys) {
      if (!seen.has(k)) {
        seen.add(k);
        out.push(k);
      }
    }
    return out;
  }

  // --- per-effect snapshot / apply / restore -----------------------------

  async snapshot(effect: Effect): Promise<SnapshotHandle> {
    const records: Record<string, KeyRecord> = {};
    for (const key of RedisAdapter.touchedKeys(effect)) {
      const dumped = await this.redis.dump(key);
      if (dumped === null || dumped === undefined) {
        records[key] = { existed: false, dump: null, pttl: -1 };
      } else {
        // PTTL: -1 means "no expiry", -2 means "key missing" (cannot happen
        // here, dump just succeeded). RESTORE wants 0 for "no expiry", so we
        // normalise at restore time.
        const pttl = Number(await this.redis.pttl(key));
        records[key] = {
          existed: true,
          dump: Buffer.from(dumped).toString("base64"),
          pttl,
        };
      }
    }
    return {
      resource: this.name,
      effectIndex: effect.index,
      payload: { keys: records },
    };
  }

  apply(effect: Effect, toolFn: ToolFn): unknown {
    // The redis client is injected as the tool's first positional arg, as
    // SqliteAdapter injects the connection. The @tool wrapper hides it.
    return toolFn(this.redis, effect.args);
  }

  async restore(handle: SnapshotHandle): Promise<void> {
    const records = handle.payload["keys"] as Record<string, KeyRecord>;
    if (Object.keys(records).length === 0) return;
    const pipe = this.redis.pipeline();
    for (const [key, record] of Object.entries(records)) {
      // Always DEL first: RESTORE refuses to overwrite an existing key (errors
      // with "BUSYKEY"), and for the absent case DEL is the whole undo. DEL on
      // a missing key is a harmless no-op.
      pipe.del(key);
      if (record.existed) {
        const ttl = record.pttl;
        // RESTORE's ttl arg is milliseconds, 0 == persist (no expiry). We
        // stored -1 ("no expiry") from PTTL; map both -1 and -2 to 0.
        const restoreTtl = ttl && ttl > 0 ? ttl : 0;
        pipe.restore(key, restoreTtl, new Uint8Array(Buffer.from(record.dump as string, "base64")));
      }
    }
    await pipe.exec();
  }
}
