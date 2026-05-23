"""Unit tests for GCSAdapter.

Google Cloud Storage has no third-party in-process fake, so these run against
the faithful in-repo double in ``tests/_fakes.py`` — a genuine snapshot ->
mutate -> restore round-trip against bytes. Mirrors ``test_adapters_s3.py``.
"""

from __future__ import annotations

import json

import pytest

from pherix.core.adapters.base import ResourceAdapter
from pherix.core.adapters.gcs import GCSAdapter
from pherix.core.effects import Effect

from tests._fakes import FakeGCSClient

BUCKET = "pherix-test-bucket"


def _effect(args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args=args,
        resource="gcs",
        reversible=True,
    )


def _snap(adapter: GCSAdapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


def _get(client, key):
    blob = client.bucket(BUCKET).blob(key)
    return blob.download_as_bytes() if blob.exists() else None


def _put(client, key, body: bytes):
    client.bucket(BUCKET).blob(key).upload_from_string(body)


@pytest.fixture
def gcs() -> FakeGCSClient:
    return FakeGCSClient()


@pytest.fixture
def adapter(gcs) -> GCSAdapter:
    return GCSAdapter(gcs, BUCKET)


# --- protocol conformance ----------------------------------------------------


def test_gcs_adapter_satisfies_resource_adapter_protocol(adapter):
    assert isinstance(adapter, ResourceAdapter)


def test_supports_rollback_is_true(adapter):
    assert adapter.supports_rollback() is True


def test_name_is_gcs(adapter):
    assert adapter.name == "gcs"


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_blob_restores_to_original_bytes(adapter, gcs):
    _put(gcs, "doc.bin", b"original")

    effect = _effect({"key": "doc.bin"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        _put(client, key, b"modified")

    adapter.apply(effect, tool)
    assert _get(gcs, "doc.bin") == b"modified"

    adapter.restore(handle)
    assert _get(gcs, "doc.bin") == b"original"


def test_created_blob_is_deleted_on_restore(adapter, gcs):
    effect = _effect({"key": "new.bin"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        _put(client, key, b"hello")

    adapter.apply(effect, tool)
    assert _get(gcs, "new.bin") == b"hello"

    adapter.restore(handle)
    assert _get(gcs, "new.bin") is None


def test_deleted_pre_existing_blob_is_recreated_on_restore(adapter, gcs):
    _put(gcs, "keep.bin", b"precious")

    effect = _effect({"key": "keep.bin"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        client.bucket(BUCKET).blob(key).delete()

    adapter.apply(effect, tool)
    assert _get(gcs, "keep.bin") is None

    adapter.restore(handle)
    assert _get(gcs, "keep.bin") == b"precious"


# --- multi-key effect --------------------------------------------------------


def test_multi_key_effect_restores_all_blobs(adapter, gcs):
    _put(gcs, "a", b"a0")
    _put(gcs, "b", b"b0")

    effect = _effect({"keys": ["a", "b", "c"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        _put(client, "a", b"a1")
        client.bucket(BUCKET).blob("b").delete()
        _put(client, "c", b"c1")  # newly created

    adapter.apply(effect, tool)
    adapter.restore(handle)

    assert _get(gcs, "a") == b"a0"
    assert _get(gcs, "b") == b"b0"
    assert _get(gcs, "c") is None


def test_partial_failure_still_restores_captured_keys(adapter, gcs):
    _put(gcs, "x", b"x0")
    _put(gcs, "y", b"y0")

    effect = _effect({"keys": ["x", "y"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        _put(client, "x", b"x1")
        raise RuntimeError("boom mid-effect")

    with pytest.raises(RuntimeError, match="boom"):
        adapter.apply(effect, tool)
    adapter.restore(handle)
    assert _get(gcs, "x") == b"x0"
    assert _get(gcs, "y") == b"y0"


# --- payload + injection -----------------------------------------------------


def test_payload_is_json_serialisable(adapter, gcs):
    _put(gcs, "p.bin", b"\x00\x01\x02bytes")
    effect = _effect({"keys": ["p.bin", "absent.bin"]})
    handle = _snap(adapter, effect)
    # base64-encoded body keeps the payload JSON-light despite raw bytes.
    json.dumps(handle.payload)


def test_apply_injects_client_as_first_arg(adapter, gcs):
    effect = _effect({"key": "z.bin"})
    _snap(adapter, effect)
    seen = {}

    def tool(client, key):
        seen["client"] = client
        seen["key"] = key

    adapter.apply(effect, tool)
    assert seen["client"] is gcs
    assert seen["key"] == "z.bin"


def test_effect_touching_no_blob_snapshots_nothing(adapter):
    effect = _effect({"unrelated": "value"})
    handle = _snap(adapter, effect)
    assert handle.payload["blobs"] == {}
    adapter.restore(handle)  # clean no-op
