"""S3Adapter — snapshot-the-touched-object reversibility over an object store.

S3 has no native savepoint or transaction model: a ``put_object`` /
``delete_object`` lands immediately and irreversibly at the storage layer. Yet
the *single object* an effect touches is small enough to copy. So reversibility
here is the same machinery as :mod:`pherix.core.adapters.filesystem` — capture
the **before-image** of the touched object(s) in :meth:`snapshot`, and write
that image back in :meth:`restore`. The object store does no undo for us (unlike
SQLite savepoints); we do it by hand, honestly, against real bytes.

Touched-keys convention (route b)
---------------------------------
The adapter learns which object(s) an effect touches by reading them off
``effect.args`` by name. The convention, fixed and documented:

- A single object: ``args["key"]`` is the object key (string).
- Multiple objects in one effect: ``args["keys"]`` is a list of object keys.

If neither is present, the effect touches no object and :meth:`snapshot`
captures nothing — restore is then a no-op. The bucket is fixed at adapter
construction (one adapter == one bucket), mirroring "one SQLiteAdapter == one
connection". A before-image records, per key, either the prior bytes (object
existed) or an *absent* marker (object did not exist) — exactly the
existed/backup split the filesystem adapter uses. On restore: existed → put the
prior bytes back; absent → delete whatever the effect created.

Atomicity (honest)
------------------
S3 gives no cross-object atomicity, and neither does this adapter: a multi-key
effect that fails partway leaves some objects mutated. That is what the journal
backward-fold is for — :meth:`restore` rewrites every captured key back to its
before-image regardless of how far ``apply`` got, landing the bucket at the
pre-effect state. Within a *single* key, ``put_object`` is atomic at the S3
layer (an object is wholly replaced or not at all).

``boto3`` is imported lazily inside methods so ``import pherix`` works with no
third-party packages installed.
"""

from __future__ import annotations

from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class S3Adapter:
    """``ResourceAdapter`` over one S3 bucket, reversible by object-snapshot."""

    name = "s3"

    def __init__(self, client: Any, bucket: str):
        # ``client`` is a boto3 S3 client (``boto3.client("s3")``); ``bucket``
        # is the single bucket this adapter speaks for. We do not import boto3
        # here — the caller already constructed the client, so the dependency
        # is theirs, lazily. Storing the handle mirrors SQLiteAdapter holding
        # its connection.
        self._client = client
        self._bucket = bucket

    @property
    def client(self) -> Any:
        return self._client

    @property
    def bucket(self) -> str:
        return self._bucket

    def supports_rollback(self) -> bool:
        return True

    # --- touched-key extraction --------------------------------------------

    @staticmethod
    def _touched_keys(effect: Effect) -> list[str]:
        """Object keys this effect touches, per the documented convention.

        ``args["key"]`` (single) and/or ``args["keys"]`` (list) are honoured;
        their union is returned, de-duplicated, order-preserving. An effect
        that names neither touches nothing.
        """
        keys: list[str] = []
        single = effect.args.get("key")
        if single is not None:
            keys.append(single)
        multi = effect.args.get("keys")
        if multi:
            keys.extend(multi)
        # De-dup while preserving first-seen order.
        seen: set[str] = set()
        out: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    # --- per-effect snapshot / apply / restore -----------------------------

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        """Capture the before-image of every touched object.

        Each key becomes a record: ``{"existed": True, "body": <b64 str>}`` if
        the object is present, else ``{"existed": False, "body": None}``. The
        body is base64-encoded so the payload stays JSON-light (the audit
        journal serialises it with ``json.dumps``).
        """
        import base64

        from botocore.exceptions import ClientError

        records: dict[str, dict] = {}
        for key in self._touched_keys(effect):
            try:
                resp = self._client.get_object(Bucket=self._bucket, Key=key)
                body = resp["Body"].read()
                records[key] = {
                    "existed": True,
                    "body": base64.b64encode(body).decode("ascii"),
                }
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                # NoSuchKey (real S3) / 404 (moto + some gateways) both mean
                # "absent" — record it so restore deletes whatever the effect
                # creates. Any other error (permissions, network) is a real
                # failure and must surface, not be silently treated as absent.
                if code in ("NoSuchKey", "404"):
                    records[key] = {"existed": False, "body": None}
                else:
                    raise
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"bucket": self._bucket, "objects": records},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The S3 client is injected as the tool's first positional arg, exactly
        # as SQLiteAdapter injects the connection. The tool calls put_object /
        # delete_object against it; the @tool wrapper hides the handle from the
        # agent's call-site.
        return tool_fn(self._client, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        """Rewrite every captured object back to its before-image.

        existed → ``put_object`` the prior bytes back; absent → ``delete_object``
        (idempotent in S3 even if the effect never created it).
        """
        import base64

        bucket = handle.payload["bucket"]
        objects: dict[str, dict] = handle.payload["objects"]
        for key, record in objects.items():
            if record["existed"]:
                body = base64.b64decode(record["body"])
                self._client.put_object(Bucket=bucket, Key=key, Body=body)
            else:
                # delete_object is a no-op (200) on a missing key in S3, so this
                # is safe whether the effect created the object or not.
                self._client.delete_object(Bucket=bucket, Key=key)
