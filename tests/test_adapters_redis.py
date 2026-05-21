"""Unit tests for RedisAdapter (Stream A2).

Exercises the adapter directly with synthesized Effects, mirroring
``test_adapters_filesystem.py``. Runs fully offline via ``fakeredis`` — a
genuine DUMP/RESTORE snapshot -> mutate -> restore round-trip, preserving value
type and TTL.
"""

from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pherix.core.adapters.base import ResourceAdapter
from pherix.core.adapters.redis import RedisAdapter
from pherix.core.effects import Effect


def _effect(args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args=args,
        resource="redis",
        reversible=True,
    )


def _snap(adapter: RedisAdapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


@pytest.fixture
def client():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def adapter(client) -> RedisAdapter:
    return RedisAdapter(client)


# --- protocol conformance ----------------------------------------------------


def test_redis_adapter_satisfies_resource_adapter_protocol(adapter: RedisAdapter):
    assert isinstance(adapter, ResourceAdapter)


def test_supports_rollback_is_true(adapter: RedisAdapter):
    assert adapter.supports_rollback() is True


def test_name_is_redis(adapter: RedisAdapter):
    assert adapter.name == "redis"


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_string_restores_to_original_value(adapter: RedisAdapter, client):
    client.set("k", b"original")

    effect = _effect({"key": "k"})
    handle = _snap(adapter, effect)

    def tool(c, key):
        c.set(key, b"modified")

    adapter.apply(effect, tool)
    assert client.get("k") == b"modified"

    adapter.restore(handle)
    assert client.get("k") == b"original"


def test_created_key_is_deleted_on_restore(adapter: RedisAdapter, client):
    effect = _effect({"key": "new"})
    handle = _snap(adapter, effect)

    def tool(c, key):
        c.set(key, b"hello")

    adapter.apply(effect, tool)
    assert client.get("new") == b"hello"

    adapter.restore(handle)
    assert client.exists("new") == 0


def test_deleted_pre_existing_key_is_recreated_on_restore(adapter: RedisAdapter, client):
    client.set("keep", b"precious")

    effect = _effect({"key": "keep"})
    handle = _snap(adapter, effect)

    def tool(c, key):
        c.delete(key)

    adapter.apply(effect, tool)
    assert client.exists("keep") == 0

    adapter.restore(handle)
    assert client.get("keep") == b"precious"


# --- type + TTL preservation (why DUMP/RESTORE, not GET/SET) -----------------


def test_hash_value_type_is_preserved_on_restore(adapter: RedisAdapter, client):
    # A GET/SET design would corrupt a non-string type. DUMP/RESTORE rebuilds
    # the exact value, hash included.
    client.hset("h", mapping={"a": "1", "b": "2"})

    effect = _effect({"key": "h"})
    handle = _snap(adapter, effect)

    def tool(c, key):
        c.delete(key)
        c.hset(key, mapping={"completely": "different"})

    adapter.apply(effect, tool)
    adapter.restore(handle)
    assert client.type("h") == b"hash"
    assert client.hgetall("h") == {b"a": b"1", b"b": b"2"}


def test_ttl_is_restored(adapter: RedisAdapter, client):
    client.set("expiring", b"v0", ex=1000)  # 1000s TTL

    effect = _effect({"key": "expiring"})
    handle = _snap(adapter, effect)

    def tool(c, key):
        c.set(key, b"v1")  # plain SET clears the TTL

    adapter.apply(effect, tool)
    assert client.ttl("expiring") == -1  # no expiry after plain SET

    adapter.restore(handle)
    assert client.get("expiring") == b"v0"
    # TTL is back (within a small tolerance of the original 1000s).
    assert 0 < client.ttl("expiring") <= 1000


# --- multi-key + adversarial -------------------------------------------------


def test_multi_key_effect_restores_all_keys(adapter: RedisAdapter, client):
    client.set("a", b"a0")
    client.set("b", b"b0")

    effect = _effect({"keys": ["a", "b", "c"]})
    handle = _snap(adapter, effect)

    def tool(c, keys):
        c.set("a", b"a1")
        c.delete("b")
        c.set("c", b"c1")  # newly created

    adapter.apply(effect, tool)
    adapter.restore(handle)

    assert client.get("a") == b"a0"
    assert client.get("b") == b"b0"
    assert client.exists("c") == 0


def test_partial_failure_still_restores_captured_keys(adapter: RedisAdapter, client):
    # Adversarial: tool mutates one key then raises. restore() lands every
    # captured key back regardless of apply completing.
    client.set("x", b"x0")
    client.set("y", b"y0")

    effect = _effect({"keys": ["x", "y"]})
    handle = _snap(adapter, effect)

    def tool(c, keys):
        c.set("x", b"x1")
        raise RuntimeError("boom mid-effect")

    with pytest.raises(RuntimeError, match="boom"):
        adapter.apply(effect, tool)
    adapter.restore(handle)
    assert client.get("x") == b"x0"
    assert client.get("y") == b"y0"


# --- payload + injection -----------------------------------------------------


def test_payload_is_json_serialisable(adapter: RedisAdapter, client):
    import json

    client.set("p", b"\x00\x01\x02raw")
    effect = _effect({"keys": ["p", "absent"]})
    handle = _snap(adapter, effect)
    json.dumps(handle.payload)


def test_apply_injects_client_as_first_arg(adapter: RedisAdapter, client):
    effect = _effect({"key": "z"})
    _snap(adapter, effect)
    seen = {}

    def tool(c, key):
        seen["client"] = c
        seen["key"] = key

    adapter.apply(effect, tool)
    assert seen["client"] is client
    assert seen["key"] == "z"


def test_effect_touching_no_key_snapshots_nothing(adapter: RedisAdapter):
    effect = _effect({"unrelated": "value"})
    handle = _snap(adapter, effect)
    assert handle.payload["keys"] == {}
    adapter.restore(handle)  # clean no-op
