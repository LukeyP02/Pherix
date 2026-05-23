"""DynamoDBAdapter — snapshot-the-touched-item reversibility over one table.

DynamoDB has no per-write savepoint: a ``PutItem`` / ``DeleteItem`` lands
immediately. But a single item is small enough to copy, so reversibility is the
S3/Redis machinery applied to items — capture the before-image of every touched
item in :meth:`snapshot` (``GetItem``), rewrite it in :meth:`restore`
(``PutItem`` if it existed, ``DeleteItem`` if it did not).

One adapter speaks for one table, addressed by a single string partition key
(``key_attr``, default ``"pk"``) — mirroring "one S3Adapter == one bucket". The
touched-key convention is route-b, identical to S3/Redis: the partition-key
value(s) come from ``args["key"]`` (single) and/or ``args["keys"]`` (list). An
effect that names neither touches nothing.

``boto3`` is imported lazily by the caller (it constructs the client); this
module never imports it, so ``import pherix`` stays dependency-free. The
before-image is the raw low-level item dict (typed-attribute form, e.g.
``{"pk": {"S": "a"}, "v": {"S": "1"}}``) — already JSON-light, so the audit
journal serialises it with no extra encoding.

Honesty caveat: like the other store adapters this is reversible (the backward
fold restores the item) but it does **not** implement the version contract, so
its effects do not participate in commit-time isolation diffing — exactly as
S3/Redis/Mongo. A design partner who needs versioned DynamoDB isolation pulls
that in (a conditional-write version attribute), the same way #8 was pulled for
SQL.
"""

from __future__ import annotations

from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class DynamoDBAdapter:
    """``ResourceAdapter`` over one DynamoDB table, reversible by item-snapshot."""

    name = "dynamodb"

    def __init__(self, client: Any, table: str, *, key_attr: str = "pk"):
        # ``client`` is a boto3 DynamoDB client (``boto3.client("dynamodb")``);
        # ``table`` is the single table this adapter speaks for; ``key_attr`` is
        # the partition-key attribute name. We do not import boto3 here — the
        # caller already constructed the client, so the dependency is theirs,
        # lazily. Storing the handle mirrors SQLiteAdapter holding its
        # connection.
        self._client = client
        self._table = table
        self._key_attr = key_attr

    @property
    def client(self) -> Any:
        return self._client

    @property
    def table(self) -> str:
        return self._table

    def supports_rollback(self) -> bool:
        return True

    # --- touched-key extraction --------------------------------------------

    @staticmethod
    def _touched_keys(effect: Effect) -> list[str]:
        """Partition-key values this effect touches, per the route-b convention.

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

    def _key_dict(self, value: str) -> dict:
        # Partition keys are addressed as strings (``{"S": value}``). A table
        # whose PK is numeric/binary is the caller's to adapt; the base contract
        # is the common string-keyed table.
        return {self._key_attr: {"S": value}}

    # --- per-effect snapshot / apply / restore -----------------------------

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        """Capture the before-image of every touched item via ``GetItem``.

        A present item is recorded as ``{"existed": True, "item": <item dict>}``;
        an absent one (``GetItem`` returns no ``Item`` — no exception) as
        ``{"existed": False, "item": None}``.
        """
        records: dict[str, dict] = {}
        for key in self._touched_keys(effect):
            resp = self._client.get_item(
                TableName=self._table, Key=self._key_dict(key)
            )
            item = resp.get("Item")
            records[key] = (
                {"existed": True, "item": item}
                if item is not None
                else {"existed": False, "item": None}
            )
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"table": self._table, "items": records},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The DynamoDB client is injected as the tool's first positional arg,
        # exactly as SQLiteAdapter injects the connection. The @tool wrapper
        # hides the handle from the agent's call-site.
        return tool_fn(self._client, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        """Rewrite every captured item back to its before-image.

        existed → ``PutItem`` the prior item (overwrites whatever the effect
        wrote, or re-creates one it deleted); absent → ``DeleteItem``
        (idempotent on a missing key).
        """
        table = handle.payload["table"]
        items: dict[str, dict] = handle.payload["items"]
        for key, record in items.items():
            if record["existed"]:
                self._client.put_item(TableName=table, Item=record["item"])
            else:
                self._client.delete_item(TableName=table, Key=self._key_dict(key))
