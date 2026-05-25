"""Unit tests for DynamoDBAdapter.

Runs fully offline via ``moto``'s in-process DynamoDB mock + a real boto3
client — a genuine snapshot -> mutate -> restore round-trip against items, not a
stub. Mirrors ``test_adapters_s3.py``.
"""

from __future__ import annotations

import json

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

from moto import mock_aws

from pherix.core.adapters.base import ResourceAdapter
from pherix.core.adapters.dynamodb import DynamoDBAdapter
from pherix.core.effects import Effect

TABLE = "pherix-test-table"


def _effect(args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args=args,
        resource="dynamodb",
        reversible=True,
    )


def _snap(adapter: DynamoDBAdapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


def _get(client, key):
    resp = client.get_item(TableName=TABLE, Key={"pk": {"S": key}})
    item = resp.get("Item")
    return None if item is None else item["v"]["S"]


def _put(client, key, value):
    client.put_item(TableName=TABLE, Item={"pk": {"S": key}, "v": {"S": value}})


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def adapter(ddb) -> DynamoDBAdapter:
    return DynamoDBAdapter(ddb, TABLE)


# --- protocol conformance ----------------------------------------------------


def test_dynamodb_adapter_satisfies_resource_adapter_protocol(adapter):
    assert isinstance(adapter, ResourceAdapter)


def test_supports_rollback_is_true(adapter):
    assert adapter.supports_rollback() is True


def test_name_is_dynamodb(adapter):
    assert adapter.name == "dynamodb"


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_item_restores_to_original(adapter, ddb):
    _put(ddb, "doc", "original")

    effect = _effect({"key": "doc"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        _put(client, key, "modified")

    adapter.apply(effect, tool)
    assert _get(ddb, "doc") == "modified"

    adapter.restore(handle)
    assert _get(ddb, "doc") == "original"


def test_created_item_is_deleted_on_restore(adapter, ddb):
    effect = _effect({"key": "new"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        _put(client, key, "hello")

    adapter.apply(effect, tool)
    assert _get(ddb, "new") == "hello"

    adapter.restore(handle)
    assert _get(ddb, "new") is None


def test_deleted_pre_existing_item_is_recreated_on_restore(adapter, ddb):
    _put(ddb, "keep", "precious")

    effect = _effect({"key": "keep"})
    handle = _snap(adapter, effect)

    def tool(client, key):
        client.delete_item(TableName=TABLE, Key={"pk": {"S": key}})

    adapter.apply(effect, tool)
    assert _get(ddb, "keep") is None

    adapter.restore(handle)
    assert _get(ddb, "keep") == "precious"


# --- multi-key effect --------------------------------------------------------


def test_multi_key_effect_restores_all_items(adapter, ddb):
    _put(ddb, "a", "a0")
    _put(ddb, "b", "b0")

    effect = _effect({"keys": ["a", "b", "c"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        _put(client, "a", "a1")
        client.delete_item(TableName=TABLE, Key={"pk": {"S": "b"}})
        _put(client, "c", "c1")  # newly created

    adapter.apply(effect, tool)
    adapter.restore(handle)

    assert _get(ddb, "a") == "a0"
    assert _get(ddb, "b") == "b0"
    assert _get(ddb, "c") is None


def test_partial_failure_still_restores_captured_keys(adapter, ddb):
    # Adversarial: the tool mutates one item, then raises before the next.
    # restore() must still land every captured key back — the backward fold does
    # not depend on apply completing.
    _put(ddb, "x", "x0")
    _put(ddb, "y", "y0")

    effect = _effect({"keys": ["x", "y"]})
    handle = _snap(adapter, effect)

    def tool(client, keys):
        _put(client, "x", "x1")
        raise RuntimeError("boom mid-effect")

    with pytest.raises(RuntimeError, match="boom"):
        adapter.apply(effect, tool)
    adapter.restore(handle)
    assert _get(ddb, "x") == "x0"
    assert _get(ddb, "y") == "y0"


# --- payload + injection -----------------------------------------------------


def test_payload_is_json_serialisable(adapter, ddb):
    _put(ddb, "p", "v")
    effect = _effect({"keys": ["p", "absent"]})
    handle = _snap(adapter, effect)
    json.dumps(handle.payload)


def test_apply_injects_client_as_first_arg(adapter, ddb):
    effect = _effect({"key": "z"})
    _snap(adapter, effect)
    seen = {}

    def tool(client, key):
        seen["client"] = client
        seen["key"] = key

    adapter.apply(effect, tool)
    assert seen["client"] is ddb
    assert seen["key"] == "z"


def test_effect_touching_no_item_snapshots_nothing(adapter):
    effect = _effect({"unrelated": "value"})
    handle = _snap(adapter, effect)
    assert handle.payload["items"] == {}
    adapter.restore(handle)  # clean no-op


def test_custom_key_attr(ddb):
    # A table whose partition key is not "pk": the adapter must address it by the
    # configured key_attr.
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="custom",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        adapter = DynamoDBAdapter(client, "custom", key_attr="id")
        client.put_item(TableName="custom", Item={"id": {"S": "k"}, "v": {"S": "0"}})

        effect = _effect({"key": "k"})
        handle = adapter.snapshot(effect)

        def tool(c, key):
            c.put_item(TableName="custom", Item={"id": {"S": "k"}, "v": {"S": "1"}})

        adapter.apply(effect, tool)
        adapter.restore(handle)
        resp = client.get_item(TableName="custom", Key={"id": {"S": "k"}})
        assert resp["Item"]["v"]["S"] == "0"
