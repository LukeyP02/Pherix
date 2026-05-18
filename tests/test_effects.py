import json
from datetime import datetime
from dataclasses import asdict, dataclass

import pytest

from pherix.core.effects import (
    Effect,
    EffectArgsError,
    EffectStatus,
    StagedResult,
    compute_effect_id,
    strict_json_default,
)


def make_effect(**overrides):
    base = dict(
        txn_id="txn-1",
        index=0,
        tool="insert_user",
        args={"name": "bob"},
        resource="sql",
        reversible=True,
    )
    base.update(overrides)
    return Effect(**base)


def test_effect_status_has_full_lifecycle():
    names = {s.name for s in EffectStatus}
    assert names == {"STAGED", "APPLIED", "COMPENSATED", "GATED", "FAILED"}


def test_effect_defaults():
    e = make_effect()
    assert e.status is EffectStatus.STAGED
    assert e.read_keys == []
    assert e.write_keys == []
    assert e.snapshot is None
    assert e.result is None
    assert e.compensator is None
    assert e.ts is not None


def test_effect_id_is_derived_when_not_supplied():
    e = make_effect()
    assert e.effect_id == compute_effect_id("txn-1", 0, "insert_user", {"name": "bob"})
    assert e.effect_id


def test_effect_id_is_deterministic_regardless_of_arg_order():
    a = compute_effect_id("txn-1", 0, "t", {"a": 1, "b": 2})
    b = compute_effect_id("txn-1", 0, "t", {"b": 2, "a": 1})
    assert a == b


def test_effect_id_varies_with_index_and_tool():
    base = compute_effect_id("txn-1", 0, "t", {"a": 1})
    assert base != compute_effect_id("txn-1", 1, "t", {"a": 1})
    assert base != compute_effect_id("txn-1", 0, "u", {"a": 1})
    assert base != compute_effect_id("txn-2", 0, "t", {"a": 1})


def test_explicit_effect_id_is_preserved():
    e = make_effect(effect_id="fixed-id")
    assert e.effect_id == "fixed-id"


def test_read_write_key_slots_accept_tuples():
    e = make_effect(read_keys=[("sql", "users:1", 3)], write_keys=[("sql", "users:1")])
    assert e.read_keys == [("sql", "users:1", 3)]
    assert e.write_keys == [("sql", "users:1")]


# --- strict args serialisation (Slice 1 review follow-up) -------------------


def test_effect_id_raises_on_non_serialisable_args():
    """Pherix's idempotency key requires deterministic serialisation —
    silent str() coercion would let two distinct non-serialisable objects
    collide on the same effect_id.
    """
    class Opaque:
        pass

    with pytest.raises(EffectArgsError, match="non-journal-able args"):
        compute_effect_id("t", 0, "tool", {"x": Opaque()})


def test_effect_construction_raises_on_non_serialisable_args():
    """The error fires at Effect construction, not later — developer sees it
    where the bad call originated."""
    with pytest.raises(EffectArgsError):
        Effect(
            txn_id="t",
            index=0,
            tool="bad",
            args={"f": lambda: None},
            resource="x",
            reversible=True,
        )


def test_effect_id_handles_bytes_args_deterministically():
    """Same bytes content → same base64 representation → same effect_id."""
    a = compute_effect_id("t", 0, "tool", {"body": b"hello"})
    b = compute_effect_id("t", 0, "tool", {"body": b"hello"})
    assert a == b
    # Different bytes → different effect_id (no collision on str-coerced repr).
    c = compute_effect_id("t", 0, "tool", {"body": b"goodbye"})
    assert a != c


def test_effect_id_handles_datetime_args_deterministically():
    when = datetime(2026, 5, 18, 12, 0, 0)
    a = compute_effect_id("t", 0, "tool", {"at": when})
    b = compute_effect_id("t", 0, "tool", {"at": when})
    assert a == b


def test_effect_id_handles_dataclass_args():
    @dataclass
    class Address:
        street: str
        city: str

    addr = Address(street="1 Pherix Way", city="London")
    a = compute_effect_id("t", 0, "tool", {"addr": addr})
    b = compute_effect_id("t", 0, "tool", {"addr": Address("1 Pherix Way", "London")})
    assert a == b


def test_strict_json_default_supports_documented_types():
    assert strict_json_default(b"x").startswith("<bytes:b64:")
    assert strict_json_default(datetime(2026, 5, 18)) == "2026-05-18T00:00:00"

    @dataclass
    class Box:
        v: int

    assert strict_json_default(Box(v=7)) == {"v": 7}


def test_strict_json_default_raises_for_unknown_types():
    class Opaque:
        pass

    with pytest.raises(TypeError, match="cannot journal"):
        strict_json_default(Opaque())


# --- StagedResult (Slice 3 / D1) -------------------------------------------


def test_staged_result_carries_effect_id():
    s = StagedResult(effect_id="abc123")
    assert s.effect_id == "abc123"


def test_staged_result_is_value_typed_and_hashable():
    # frozen=True: two StagedResults with the same effect_id are equal,
    # hashable, and immutable. Pherix relies on this for set membership in
    # approval-tracking and for safe propagation through agent code.
    a = StagedResult(effect_id="x")
    b = StagedResult(effect_id="x")
    c = StagedResult(effect_id="y")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert {a, b, c} == {a, c}


def test_staged_result_is_json_serialisable_via_asdict():
    # The audit journal serialises results with strict_json_default; for a
    # dataclass that path is asdict. We pin the JSON shape so the audit row
    # remains readable from outside the running process.
    s = StagedResult(effect_id="xyz")
    payload = json.dumps(asdict(s), sort_keys=True)
    assert json.loads(payload) == {"effect_id": "xyz"}


def test_staged_result_repr_is_useful():
    s = StagedResult(effect_id="zzz")
    assert "StagedResult" in repr(s)
    assert "zzz" in repr(s)
