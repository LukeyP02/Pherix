"""GCSAdapter — snapshot-the-touched-blob reversibility over one GCS bucket.

Google Cloud Storage has no object-level savepoint: an upload / delete lands
immediately. But a blob's bytes are copyable, so reversibility is the S3-adapter
machinery applied to GCS — capture the before-image of every touched blob in
:meth:`snapshot`, rewrite it in :meth:`restore` (re-upload the prior bytes if it
existed, delete it if it did not).

One adapter speaks for one bucket, mirroring "one S3Adapter == one bucket". The
touched-key convention is route-b, identical to S3: blob name(s) come from
``args["key"]`` (single) and/or ``args["keys"]`` (list).

Absence is decided with ``blob.exists()`` rather than catching the client's
``NotFound`` — this keeps the kernel free of any compile-time knowledge of
``google.cloud``'s exception module path (the dependency is the caller's, pulled
lazily via the ``pherix[gcs]`` extra). The cost is one extra metadata round-trip
per touched blob, which is negligible against the upload it guards.

``google-cloud-storage`` is never imported by this module; the caller constructs
the client, so ``import pherix`` stays dependency-free.

Honesty caveat: reversible (the backward fold restores the blob), but no version
contract — so GCS effects do not participate in commit-time isolation diffing,
exactly as S3/Redis/Mongo. A design partner needing versioned GCS isolation
pulls that in (object generation numbers), the same way #8 was pulled for SQL.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class GCSAdapter:
    """``ResourceAdapter`` over one GCS bucket, reversible by blob-snapshot."""

    name = "gcs"

    def __init__(self, client: Any, bucket: str):
        # ``client`` is a ``google.cloud.storage.Client``; ``bucket`` is the
        # single bucket name this adapter speaks for. Held as SQLiteAdapter
        # holds its connection; no import of google-cloud-storage happens here.
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
        """Blob names this effect touches, per the route-b convention.

        ``args["key"]`` (single) and/or ``args["keys"]`` (list) are honoured;
        their union is returned, de-duplicated, order-preserving.
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
        """Capture the before-image (bytes) of every touched blob.

        Present → ``{"existed": True, "body": <b64 str>}`` (base64 keeps the
        payload JSON-light for the audit journal); absent → ``{"existed":
        False, "body": None}``.
        """
        bucket = self._client.bucket(self._bucket)
        records: dict[str, dict] = {}
        for key in self._touched_keys(effect):
            blob = bucket.blob(key)
            if blob.exists():
                body = blob.download_as_bytes()
                records[key] = {
                    "existed": True,
                    "body": base64.b64encode(body).decode("ascii"),
                }
            else:
                records[key] = {"existed": False, "body": None}
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"bucket": self._bucket, "blobs": records},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The GCS client is injected as the tool's first positional arg, exactly
        # as SQLiteAdapter injects the connection. The @tool wrapper hides the
        # handle from the agent's call-site.
        return tool_fn(self._client, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        """Rewrite every captured blob back to its before-image.

        existed → ``upload_from_string`` the prior bytes (overwrites or
        re-creates); absent → delete the blob, guarded by ``exists()`` so the
        undo is idempotent whether or not the effect created it.
        """
        bucket = self._client.bucket(handle.payload["bucket"])
        blobs: dict[str, dict] = handle.payload["blobs"]
        for key, record in blobs.items():
            blob = bucket.blob(key)
            if record["existed"]:
                blob.upload_from_string(base64.b64decode(record["body"]))
            elif blob.exists():
                blob.delete()
