"""Unit tests for S3Adapter (Stream A2).

Exercises the adapter directly with synthesized Effects, mirroring
``test_adapters_filesystem.py``. Runs fully offline via ``moto``'s in-process
S3 mock + a real boto3 client — a genuine snapshot -> mutate -> restore
round-trip against bytes, not a stub.
"""

from __future__ import annotations

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from moto import mock_aws

from pherix.core.adapters.base import ResourceAdapter
from pherix.core.adapters.s3 import S3Adapter
from pherix.core.effects import Effect

BUCKET = "pherix-test-bucket"


def _effect(args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args=args,
        resource="s3",
        reversible=True,
    )


def _snap(adapter: S3Adapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def adapter(s3) -> S3Adapter:
    return S3Adapter(s3, BUCKET)


# --- protocol conformance ----------------------------------------------------


def test_s3_adapter_satisfies_resource_adapter_protocol(adapter: S3Adapter):
    assert isinstance(adapter, ResourceAdapter)


def test_supports_rollback_is_true(adapter: S3Adapter):
    assert adapter.supports_rollback() is True


def test_name_is_s3(adapter: S3Adapter):
    assert adapter.name == "s3"


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_object_restores_to_original_bytes(adapter: S3Adapter, s3):
    s3.put_object(Bucket=BUCKET, Key="doc.bin", Body=b"original")

    effect = _effect({"key": "doc.bin"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        client.put_object(Bucket=BUCKET, Key=key, Body=b"modified")

    adapter.apply(effect, tool)
    assert s3.get_object(Bucket=BUCKET, Key="doc.bin")["Body"].read() == b"modified"

    adapter.restore(handle)
    assert s3.get_object(Bucket=BUCKET, Key="doc.bin")["Body"].read() == b"original"


def test_created_object_is_deleted_on_restore(adapter: S3Adapter, s3):
    effect = _effect({"key": "new.bin"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        client.put_object(Bucket=BUCKET, Key=key, Body=b"hello")

    adapter.apply(effect, tool)
    assert s3.get_object(Bucket=BUCKET, Key="new.bin")["Body"].read() == b"hello"

    adapter.restore(handle)
    with pytest.raises(s3.exceptions.NoSuchKey):
        s3.get_object(Bucket=BUCKET, Key="new.bin")


def test_deleted_pre_existing_object_is_recreated_on_restore(adapter: S3Adapter, s3):
    s3.put_object(Bucket=BUCKET, Key="keep.bin", Body=b"precious")

    effect = _effect({"key": "keep.bin"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        client.delete_object(Bucket=BUCKET, Key=key)

    adapter.apply(effect, tool)
    with pytest.raises(s3.exceptions.NoSuchKey):
        s3.get_object(Bucket=BUCKET, Key="keep.bin")

    adapter.restore(handle)
    assert s3.get_object(Bucket=BUCKET, Key="keep.bin")["Body"].read() == b"precious"


# --- multi-key effect --------------------------------------------------------


def test_multi_key_effect_restores_all_objects(adapter: S3Adapter, s3):
    s3.put_object(Bucket=BUCKET, Key="a", Body=b"a0")
    s3.put_object(Bucket=BUCKET, Key="b", Body=b"b0")

    effect = _effect({"keys": ["a", "b", "c"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        client.put_object(Bucket=BUCKET, Key="a", Body=b"a1")
        client.delete_object(Bucket=BUCKET, Key="b")
        client.put_object(Bucket=BUCKET, Key="c", Body=b"c1")  # newly created

    adapter.apply(effect, tool)
    adapter.restore(handle)

    assert s3.get_object(Bucket=BUCKET, Key="a")["Body"].read() == b"a0"
    assert s3.get_object(Bucket=BUCKET, Key="b")["Body"].read() == b"b0"
    with pytest.raises(s3.exceptions.NoSuchKey):
        s3.get_object(Bucket=BUCKET, Key="c")


def test_partial_failure_still_restores_captured_keys(adapter: S3Adapter, s3):
    # Adversarial: the tool mutates one object, then raises before touching the
    # next. restore() must still land every captured key back at its
    # before-image — the backward fold does not depend on apply completing.
    s3.put_object(Bucket=BUCKET, Key="x", Body=b"x0")
    s3.put_object(Bucket=BUCKET, Key="y", Body=b"y0")

    effect = _effect({"keys": ["x", "y"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        client.put_object(Bucket=BUCKET, Key="x", Body=b"x1")
        raise RuntimeError("boom mid-effect")

    with pytest.raises(RuntimeError, match="boom"):
        adapter.apply(effect, tool)
    # x was mutated, y untouched; restore brings x back regardless.
    adapter.restore(handle)
    assert s3.get_object(Bucket=BUCKET, Key="x")["Body"].read() == b"x0"
    assert s3.get_object(Bucket=BUCKET, Key="y")["Body"].read() == b"y0"


# --- payload + injection -----------------------------------------------------


def test_payload_is_json_serialisable(adapter: S3Adapter, s3):
    import json

    s3.put_object(Bucket=BUCKET, Key="p.bin", Body=b"\x00\x01\x02bytes")
    effect = _effect({"keys": ["p.bin", "absent.bin"]})
    handle = _snap(adapter, effect)
    # base64-encoded body keeps the payload JSON-light despite raw bytes.
    json.dumps(handle.payload)


def test_apply_injects_client_as_first_arg(adapter: S3Adapter, s3):
    effect = _effect({"key": "z.bin"})
    _snap(adapter, effect)
    seen = {}

    def tool(client, key):
        seen["client"] = client
        seen["key"] = key

    adapter.apply(effect, tool)
    assert seen["client"] is s3
    assert seen["key"] == "z.bin"


def test_snapshot_propagates_non_404_client_error(adapter: S3Adapter):
    # Adversarial: a get_object failure that is NOT a missing key (e.g. the
    # bucket doesn't exist) must surface, not be silently treated as "absent".
    from botocore.exceptions import ClientError

    bad = S3Adapter(adapter.client, "no-such-bucket-at-all")
    effect = _effect({"key": "whatever"})
    with pytest.raises(ClientError):
        bad.snapshot(effect)


def test_effect_touching_no_object_snapshots_nothing(adapter: S3Adapter):
    effect = _effect({"unrelated": "value"})
    handle = _snap(adapter, effect)
    assert handle.payload["objects"] == {}
    # restore is a clean no-op.
    adapter.restore(handle)
