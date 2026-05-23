"""ElasticsearchAdapter — snapshot-the-touched-document reversibility, one index.

Elasticsearch has no document-level savepoint: an ``index`` / ``delete`` lands
immediately (and becomes searchable after a refresh). But a document's
``_source`` is copyable, so reversibility is the Mongo-adapter machinery applied
to ES — capture the before-image of every touched document in :meth:`snapshot`,
rewrite it in :meth:`restore` (re-index the prior source if it existed, delete
it if it did not).

One adapter speaks for one index, mirroring "one S3Adapter == one bucket". The
touched-key convention is route-b: document id(s) come from ``args["key"]``
(single) and/or ``args["keys"]`` (list).

Absence is decided with ``client.exists(...)`` rather than catching
``NotFoundError`` — keeping the kernel free of compile-time knowledge of the
``elasticsearch`` package (the dependency is the caller's, pulled lazily via the
``pherix[elasticsearch]`` extra). Restore writes pass ``refresh=True`` so the
undo is immediately visible to a subsequent read within the same fold; ES is
near-real-time, and an unrefreshed restore could otherwise be missed by the
next snapshot/read.

``elasticsearch`` is never imported by this module; the caller constructs the
client, so ``import pherix`` stays dependency-free. The same adapter speaks to
OpenSearch via the API-compatible ``opensearch-py`` client.

Honesty caveat: reversible (the backward fold restores the document), but no
version contract — so ES effects do not participate in commit-time isolation
diffing, exactly as S3/Redis/Mongo. A design partner needing versioned ES
isolation pulls that in (the ``_seq_no``/``_primary_term`` optimistic-concurrency
tags), the same way #8 was pulled for SQL.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class ElasticsearchAdapter:
    """``ResourceAdapter`` over one ES index, reversible by document-snapshot."""

    name = "elasticsearch"

    def __init__(self, client: Any, index: str):
        # ``client`` is an ``elasticsearch.Elasticsearch`` (or API-compatible
        # ``opensearchpy.OpenSearch``); ``index`` is the single index this
        # adapter speaks for. Held as SQLiteAdapter holds its connection; no
        # import of elasticsearch happens here.
        self._client = client
        self._index = index

    @property
    def client(self) -> Any:
        return self._client

    @property
    def index(self) -> str:
        return self._index

    def supports_rollback(self) -> bool:
        return True

    # --- touched-key extraction --------------------------------------------

    @staticmethod
    def _touched_keys(effect: Effect) -> list[str]:
        """Document ids this effect touches, per the route-b convention.

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
        """Capture the before-image (``_source``) of every touched document.

        Present → ``{"existed": True, "doc": <deep-copied source>}`` (the
        deep-copy guards against the effect mutating the dict in place); absent →
        ``{"existed": False, "doc": None}``. Sources are assumed JSON-light, the
        same contract the Mongo adapter carries.
        """
        records: dict[str, dict] = {}
        for doc_id in self._touched_keys(effect):
            if self._client.exists(index=self._index, id=doc_id):
                resp = self._client.get(index=self._index, id=doc_id)
                records[doc_id] = {
                    "existed": True,
                    "doc": copy.deepcopy(resp["_source"]),
                }
            else:
                records[doc_id] = {"existed": False, "doc": None}
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"index": self._index, "docs": records},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The ES client is injected as the tool's first positional arg, exactly
        # as SQLiteAdapter injects the connection. The @tool wrapper hides the
        # handle from the agent's call-site.
        return tool_fn(self._client, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        """Rewrite every captured document back to its before-image.

        existed → re-``index`` the prior source under the same id (overwrites or
        re-creates); absent → delete it, guarded by ``exists()`` so the undo is
        idempotent. Both write paths pass ``refresh=True`` so the restored state
        is immediately visible.
        """
        index = handle.payload["index"]
        docs: dict[str, dict] = handle.payload["docs"]
        for doc_id, record in docs.items():
            if record["existed"]:
                self._client.index(
                    index=index, id=doc_id, document=record["doc"], refresh=True
                )
            elif self._client.exists(index=index, id=doc_id):
                self._client.delete(index=index, id=doc_id, refresh=True)
