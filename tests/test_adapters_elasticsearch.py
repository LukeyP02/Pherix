"""Unit tests for ElasticsearchAdapter.

Elasticsearch has no third-party in-process fake, so these run against the
faithful in-repo double in ``tests/_fakes.py`` — a genuine snapshot -> mutate ->
restore round-trip against documents. Mirrors ``test_adapters_s3.py``.
"""

from __future__ import annotations

import json

import pytest

from pherix.core.adapters.base import ResourceAdapter
from pherix.core.adapters.elasticsearch import ElasticsearchAdapter
from pherix.core.effects import Effect

from tests._fakes import FakeESClient

INDEX = "pherix-test-index"


def _effect(args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args=args,
        resource="elasticsearch",
        reversible=True,
    )


def _snap(adapter: ElasticsearchAdapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


def _get(client, doc_id):
    if not client.exists(index=INDEX, id=doc_id):
        return None
    return client.get(index=INDEX, id=doc_id)["_source"]


def _put(client, doc_id, value):
    client.index(index=INDEX, id=doc_id, document={"v": value}, refresh=True)


@pytest.fixture
def es() -> FakeESClient:
    return FakeESClient()


@pytest.fixture
def adapter(es) -> ElasticsearchAdapter:
    return ElasticsearchAdapter(es, INDEX)


# --- protocol conformance ----------------------------------------------------


def test_es_adapter_satisfies_resource_adapter_protocol(adapter):
    assert isinstance(adapter, ResourceAdapter)


def test_supports_rollback_is_true(adapter):
    assert adapter.supports_rollback() is True


def test_name_is_elasticsearch(adapter):
    assert adapter.name == "elasticsearch"


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_doc_restores_to_original(adapter, es):
    _put(es, "doc", "original")

    effect = _effect({"key": "doc"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        _put(client, key, "modified")

    adapter.apply(effect, tool)
    assert _get(es, "doc") == {"v": "modified"}

    adapter.restore(handle)
    assert _get(es, "doc") == {"v": "original"}


def test_created_doc_is_deleted_on_restore(adapter, es):
    effect = _effect({"key": "new"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        _put(client, key, "hello")

    adapter.apply(effect, tool)
    assert _get(es, "new") == {"v": "hello"}

    adapter.restore(handle)
    assert _get(es, "new") is None


def test_deleted_pre_existing_doc_is_recreated_on_restore(adapter, es):
    _put(es, "keep", "precious")

    effect = _effect({"key": "keep"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        client.delete(index=INDEX, id=key, refresh=True)

    adapter.apply(effect, tool)
    assert _get(es, "keep") is None

    adapter.restore(handle)
    assert _get(es, "keep") == {"v": "precious"}


# --- multi-key effect --------------------------------------------------------


def test_multi_key_effect_restores_all_docs(adapter, es):
    _put(es, "a", "a0")
    _put(es, "b", "b0")

    effect = _effect({"keys": ["a", "b", "c"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        _put(client, "a", "a1")
        client.delete(index=INDEX, id="b", refresh=True)
        _put(client, "c", "c1")  # newly created

    adapter.apply(effect, tool)
    adapter.restore(handle)

    assert _get(es, "a") == {"v": "a0"}
    assert _get(es, "b") == {"v": "b0"}
    assert _get(es, "c") is None


def test_partial_failure_still_restores_captured_keys(adapter, es):
    _put(es, "x", "x0")
    _put(es, "y", "y0")

    effect = _effect({"keys": ["x", "y"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        _put(client, "x", "x1")
        raise RuntimeError("boom mid-effect")

    with pytest.raises(RuntimeError, match="boom"):
        adapter.apply(effect, tool)
    adapter.restore(handle)
    assert _get(es, "x") == {"v": "x0"}
    assert _get(es, "y") == {"v": "y0"}


# --- payload + injection -----------------------------------------------------


def test_payload_is_json_serialisable(adapter, es):
    _put(es, "p", "v")
    effect = _effect({"keys": ["p", "absent"]})
    handle = _snap(adapter, effect)
    json.dumps(handle.payload)


def test_snapshot_deep_copies_source(adapter, es):
    # Mutating the live document after snapshot must not change the captured
    # before-image — the snapshot deep-copies the source.
    _put(es, "d", "before")
    effect = _effect({"key": "d"})
    handle = _snap(adapter, effect)
    _put(es, "d", "after")
    assert handle.payload["docs"]["d"]["doc"] == {"v": "before"}


def test_apply_injects_client_as_first_arg(adapter, es):
    effect = _effect({"key": "z"})
    _snap(adapter, effect)
    seen = {}

    def tool(client, key):
        seen["client"] = client
        seen["key"] = key

    adapter.apply(effect, tool)
    assert seen["client"] is es
    assert seen["key"] == "z"


def test_effect_touching_no_doc_snapshots_nothing(adapter):
    effect = _effect({"unrelated": "value"})
    handle = _snap(adapter, effect)
    assert handle.payload["docs"] == {}
    adapter.restore(handle)  # clean no-op
