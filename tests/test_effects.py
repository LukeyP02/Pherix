import json
from dataclasses import asdict

from pherix.core.effects import Effect, EffectStatus, StagedResult, compute_effect_id


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


# --- StagedResult (Slice 3 / D1) ---


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
    # The audit journal serialises results with _json_default; for a
    # dataclass that path is asdict. We pin the JSON shape so the audit row
    # remains readable from outside the running process.
    s = StagedResult(effect_id="xyz")
    payload = json.dumps(asdict(s), sort_keys=True)
    assert json.loads(payload) == {"effect_id": "xyz"}


def test_staged_result_repr_is_useful():
    s = StagedResult(effect_id="zzz")
    assert "StagedResult" in repr(s)
    assert "zzz" in repr(s)
