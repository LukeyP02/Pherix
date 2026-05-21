"""RedisAdapter — snapshot-the-touched-key reversibility over a key-value store.

Redis has no per-command savepoint: a ``SET`` / ``DEL`` / ``HSET`` lands
immediately. But the *value* under a touched key is small enough to copy. So
reversibility is again the filesystem-adapter machinery — capture the
**before-image** of each touched key in :meth:`snapshot`, write it back in
:meth:`restore`.

Why DUMP/RESTORE, not GET/SET
-----------------------------
``GET`` only works on string keys; an effect might touch a hash, list, set, or
sorted set. ``DUMP`` returns the Redis-internal serialisation of *any* value
type, and ``RESTORE`` rebuilds it exactly — preserving the value's type. We pair
it with reading ``PTTL`` so the key's expiry is restored too. The before-image
per key is therefore: ``{"existed": True, "dump": <b64>, "pttl": <ms or -1>}``
or ``{"existed": False}``.

Touched-keys convention (route b)
---------------------------------
The adapter learns the touched key(s) from ``effect.args`` by name:

- A single key: ``args["key"]``.
- Multiple keys in one effect: ``args["keys"]`` (a list).

If neither is present, nothing is captured and restore is a no-op.

Atomicity (honest)
------------------
A multi-key effect is not atomic at the Redis layer (each command lands
independently); the journal backward-fold is what makes the *effect* atomic —
:meth:`restore` rewrites every captured key back regardless of how far ``apply``
got. The :meth:`restore` itself is wrapped in a single ``MULTI``/``EXEC``
pipeline so the *undo* of one effect is applied as one round-trip and is not
interleaved with another client's commands. (``MULTI`` gives all-or-nothing
queuing, not rollback-on-error — Redis has no rollback — but for restore, which
is a fixed list of writes computed from the before-image, that is sufficient.)

``redis`` is imported lazily inside methods so ``import pherix`` works with no
third-party packages installed.
"""

from __future__ import annotations

from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class RedisAdapter:
    """``ResourceAdapter`` over a Redis client, reversible by key-snapshot."""

    name = "redis"

    def __init__(self, client: Any):
        # ``client`` is a redis client (``redis.Redis(...)`` or a compatible
        # fake). We hold it as SQLiteAdapter holds its connection; no import of
        # the ``redis`` package happens here.
        self._client = client

    @property
    def client(self) -> Any:
        return self._client

    def supports_rollback(self) -> bool:
        return True

    # --- touched-key extraction --------------------------------------------

    @staticmethod
    def _touched_keys(effect: Effect) -> list[str]:
        """Keys this effect touches, per the documented convention.

        Union of ``args["key"]`` (single) and ``args["keys"]`` (list),
        de-duplicated, order-preserving.
        """
        keys: list[str] = []
        single = effect.args.get("key")
        if single is not None:
            keys.append(single)
        multi = effect.args.get("keys")
        if multi:
            keys.extend(multi)
        seen: set[str] = set()
        out: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    # --- per-effect snapshot / apply / restore -----------------------------

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        """Capture the before-image (DUMP + PTTL) of every touched key.

        ``DUMP`` returns ``None`` for an absent key — recorded as
        ``{"existed": False}``. For a present key the raw dump bytes are
        base64-encoded so the payload is JSON-light, and the millisecond TTL is
        captured so :meth:`restore` rebuilds the expiry too.
        """
        import base64

        records: dict[str, dict] = {}
        for key in self._touched_keys(effect):
            dumped = self._client.dump(key)
            if dumped is None:
                records[key] = {"existed": False, "dump": None, "pttl": -1}
            else:
                # PTTL: -1 means "no expiry", -2 means "key missing" (cannot
                # happen here, dump just succeeded). RESTORE wants 0 for "no
                # expiry", so we normalise -1 -> 0 at restore time.
                pttl = int(self._client.pttl(key))
                records[key] = {
                    "existed": True,
                    "dump": base64.b64encode(dumped).decode("ascii"),
                    "pttl": pttl,
                }
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"keys": records},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The redis client is injected as the tool's first positional arg, as
        # SQLiteAdapter injects the connection. The @tool wrapper hides it from
        # the agent's call-site.
        return tool_fn(self._client, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        """Rewrite every captured key back to its before-image.

        existed → ``DEL`` then ``RESTORE`` the dumped value (with its prior
        TTL); absent → ``DEL`` (removes whatever the effect created). The whole
        undo runs inside one ``MULTI``/``EXEC`` transaction so it is one
        round-trip and not interleaved with other clients.
        """
        import base64

        records: dict[str, dict] = handle.payload["keys"]
        if not records:
            return
        pipe = self._client.pipeline(transaction=True)
        for key, record in records.items():
            # Always DEL first: RESTORE refuses to overwrite an existing key
            # (errors with "BUSYKEY"), and for the absent case DEL is the whole
            # undo. DEL on a missing key is a harmless no-op.
            pipe.delete(key)
            if record["existed"]:
                ttl = record["pttl"]
                # RESTORE's ttl arg is milliseconds, 0 == persist (no expiry).
                # We stored -1 ("no expiry") from PTTL; map both -1 and -2 to 0.
                restore_ttl = ttl if ttl and ttl > 0 else 0
                pipe.restore(key, restore_ttl, base64.b64decode(record["dump"]))
        pipe.execute()
