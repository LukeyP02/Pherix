"""In-process fakes for adapters whose ecosystem has no maintained fake double.

S3/Redis/Mongo are tested against third-party in-process fakes (moto, fakeredis,
mongomock) and DynamoDB against moto. Google Cloud Storage and Elasticsearch
have no equivalent in-process fake on PyPI, so this module ships minimal, FAITHFUL
doubles modelled strictly on each client's documented surface — exactly the slice
the adapter touches and nothing more. The conformance battery and the per-adapter
unit tests both run against these, so the round-trip law (mutate → restore →
byte-identical) is exercised offline rather than skipped.

The leading underscore keeps pytest from collecting this module (consistent with
``_conformance.py`` / ``_laws.py``).

These doubles are deliberately tiny: they prove the adapter's snapshot/restore
*algebra*, not the backend's storage engine. Production uses the real client; the
real-driver path is unchanged.
"""

from __future__ import annotations

import copy
from typing import Any


# ---------------------------------------------------------------------------
# Google Cloud Storage — models the ``client.bucket(name).blob(key)`` surface
# the GCSAdapter uses: ``exists`` / ``download_as_bytes`` / ``upload_from_string``
# / ``delete``, plus ``client.list_blobs`` for the conformance dump.
# ---------------------------------------------------------------------------


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name

    def exists(self) -> bool:
        return self.name in self._store

    def download_as_bytes(self) -> bytes:
        # Real GCS raises NotFound here on a missing blob; the adapter only
        # calls this after ``exists()`` is True, so a plain lookup suffices.
        return self._store[self.name]

    def upload_from_string(self, data: Any) -> None:
        # Real GCS accepts str or bytes, encoding str as UTF-8.
        self._store[self.name] = data if isinstance(data, bytes) else str(data).encode()

    def delete(self) -> None:
        # Real GCS raises NotFound on a missing blob; the adapter guards delete
        # with ``exists()``, so this is only reached for a present blob.
        del self._store[self.name]


class _FakeBucket:
    def __init__(self, store: dict):
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)


class FakeGCSClient:
    """A faithful double of ``google.cloud.storage.Client`` for the adapter's slice."""

    def __init__(self) -> None:
        self._buckets: dict[str, dict] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._buckets.setdefault(name, {}))

    def list_blobs(self, bucket_or_name: Any) -> list[_FakeBlob]:
        name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        store = self._buckets.setdefault(name, {})
        return [_FakeBlob(store, n) for n in list(store)]


# ---------------------------------------------------------------------------
# Elasticsearch — models the ``exists`` / ``get`` / ``index`` / ``delete`` /
# ``search`` surface the ElasticsearchAdapter uses. Documents are stored by
# (index, id) → ``_source`` dict. ``refresh`` is accepted and ignored (the fake
# is synchronous, so writes are immediately visible — the property the adapter's
# ``refresh=True`` buys against the real near-real-time engine).
# ---------------------------------------------------------------------------


class FakeESClient:
    """A faithful double of ``elasticsearch.Elasticsearch`` for the adapter's slice."""

    def __init__(self) -> None:
        self._indices: dict[str, dict[str, dict]] = {}

    def exists(self, *, index: str, id: str) -> bool:
        return id in self._indices.get(index, {})

    def get(self, *, index: str, id: str) -> dict:
        # The adapter only calls this after ``exists()`` is True.
        source = self._indices[index][id]
        return {"_index": index, "_id": id, "found": True, "_source": copy.deepcopy(source)}

    def index(self, *, index: str, id: str, document: dict, refresh: Any = None) -> dict:
        self._indices.setdefault(index, {})[id] = copy.deepcopy(document)
        return {"_index": index, "_id": id, "result": "created"}

    def delete(self, *, index: str, id: str, refresh: Any = None) -> dict:
        # Real ES raises NotFoundError on a missing doc; the adapter guards
        # delete with ``exists()``, so this is only reached for a present doc.
        del self._indices.setdefault(index, {})[id]
        return {"_index": index, "_id": id, "result": "deleted"}

    def search(self, *, index: str, query: Any = None, **kw: Any) -> dict:
        docs = self._indices.get(index, {})
        hits = [
            {"_index": index, "_id": i, "_source": copy.deepcopy(s)}
            for i, s in docs.items()
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}
