"""Unit tests for MongoAdapter (Stream A2).

Exercises the adapter directly with synthesized Effects, mirroring
``test_adapters_filesystem.py``. Runs fully offline via ``mongomock`` — a
genuine document-snapshot -> mutate -> restore round-trip using ordinary CRUD,
so it needs no replica set / no real server.

A skip-gated test against a real pymongo server is included for anyone who sets
``PHERIX_TEST_MONGO_URI``.
"""

from __future__ import annotations

import os

import pytest

mongomock = pytest.importorskip("mongomock")

from pherix.core.adapters.base import ResourceAdapter
from pherix.core.adapters.mongodb import MongoAdapter
from pherix.core.effects import Effect


def _effect(args: dict, index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args=args,
        resource="mongodb",
        reversible=True,
    )


def _snap(adapter: MongoAdapter, effect: Effect):
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


@pytest.fixture
def db():
    return mongomock.MongoClient().testdb


@pytest.fixture
def adapter(db) -> MongoAdapter:
    return MongoAdapter(db)


# --- protocol conformance ----------------------------------------------------


def test_mongo_adapter_satisfies_resource_adapter_protocol(adapter: MongoAdapter):
    assert isinstance(adapter, ResourceAdapter)


def test_supports_rollback_is_true(adapter: MongoAdapter):
    assert adapter.supports_rollback() is True


def test_name_is_mongodb(adapter: MongoAdapter):
    assert adapter.name == "mongodb"


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_document_restores_to_original(adapter: MongoAdapter, db):
    db.users.insert_one({"_id": "u1", "name": "alice", "tier": "free"})

    effect = _effect({"collection": "users", "doc_id": "u1"})
    handle = _snap(adapter, effect)

    def tool(database, collection, doc_id):
        database[collection].update_one(
            {"_id": doc_id}, {"$set": {"tier": "premium"}}
        )

    adapter.apply(effect, tool)
    assert db.users.find_one({"_id": "u1"})["tier"] == "premium"

    adapter.restore(handle)
    restored = db.users.find_one({"_id": "u1"})
    assert restored == {"_id": "u1", "name": "alice", "tier": "free"}


def test_inserted_document_is_deleted_on_restore(adapter: MongoAdapter, db):
    effect = _effect({"collection": "users", "doc_id": "u2"})
    handle = _snap(adapter, effect)

    def tool(database, collection, doc_id):
        database[collection].insert_one({"_id": doc_id, "name": "bob"})

    adapter.apply(effect, tool)
    assert db.users.find_one({"_id": "u2"}) is not None

    adapter.restore(handle)
    assert db.users.find_one({"_id": "u2"}) is None


def test_deleted_pre_existing_document_is_recreated_on_restore(adapter: MongoAdapter, db):
    db.users.insert_one({"_id": "u3", "name": "carol", "score": 42})

    effect = _effect({"collection": "users", "doc_id": "u3"})
    handle = _snap(adapter, effect)

    def tool(database, collection, doc_id):
        database[collection].delete_one({"_id": doc_id})

    adapter.apply(effect, tool)
    assert db.users.find_one({"_id": "u3"}) is None

    adapter.restore(handle)
    assert db.users.find_one({"_id": "u3"}) == {
        "_id": "u3",
        "name": "carol",
        "score": 42,
    }


def test_snapshot_deep_copies_so_inplace_mutation_does_not_corrupt(adapter: MongoAdapter, db):
    # Adversarial: the tool mutates the document object in place. The snapshot
    # must hold an independent deep copy, or restore would write back the
    # mutated value.
    db.docs.insert_one({"_id": "d1", "items": ["a", "b"]})

    effect = _effect({"collection": "docs", "doc_id": "d1"})
    handle = _snap(adapter, effect)

    def tool(database, collection, doc_id):
        database[collection].update_one(
            {"_id": doc_id}, {"$set": {"items": ["x", "y", "z"]}}
        )

    adapter.apply(effect, tool)
    adapter.restore(handle)
    assert db.docs.find_one({"_id": "d1"})["items"] == ["a", "b"]


# --- multi-document + adversarial --------------------------------------------


def test_multi_document_effect_restores_all(adapter: MongoAdapter, db):
    db.coll.insert_one({"_id": "a", "v": 0})
    db.coll.insert_one({"_id": "b", "v": 0})

    effect = _effect(
        {
            "docs": [
                {"collection": "coll", "doc_id": "a"},
                {"collection": "coll", "doc_id": "b"},
                {"collection": "coll", "doc_id": "c"},
            ]
        }
    )
    handle = _snap(adapter, effect)

    def tool(database, docs):
        database.coll.update_one({"_id": "a"}, {"$set": {"v": 1}})
        database.coll.delete_one({"_id": "b"})
        database.coll.insert_one({"_id": "c", "v": 1})  # newly created

    adapter.apply(effect, tool)
    adapter.restore(handle)

    assert db.coll.find_one({"_id": "a"}) == {"_id": "a", "v": 0}
    assert db.coll.find_one({"_id": "b"}) == {"_id": "b", "v": 0}
    assert db.coll.find_one({"_id": "c"}) is None


def test_partial_failure_still_restores_captured_docs(adapter: MongoAdapter, db):
    db.coll.insert_one({"_id": "x", "v": 0})
    db.coll.insert_one({"_id": "y", "v": 0})

    effect = _effect(
        {
            "docs": [
                {"collection": "coll", "doc_id": "x"},
                {"collection": "coll", "doc_id": "y"},
            ]
        }
    )
    handle = _snap(adapter, effect)

    def tool(database, docs):
        database.coll.update_one({"_id": "x"}, {"$set": {"v": 99}})
        raise RuntimeError("boom mid-effect")

    with pytest.raises(RuntimeError, match="boom"):
        adapter.apply(effect, tool)
    adapter.restore(handle)
    assert db.coll.find_one({"_id": "x"}) == {"_id": "x", "v": 0}
    assert db.coll.find_one({"_id": "y"}) == {"_id": "y", "v": 0}


# --- payload + injection -----------------------------------------------------


def test_payload_is_json_serialisable(adapter: MongoAdapter, db):
    import json

    db.users.insert_one({"_id": "p", "name": "x", "n": 5})
    effect = _effect(
        {
            "docs": [
                {"collection": "users", "doc_id": "p"},
                {"collection": "users", "doc_id": "absent"},
            ]
        }
    )
    handle = _snap(adapter, effect)
    json.dumps(handle.payload)


def test_apply_injects_db_as_first_arg(adapter: MongoAdapter, db):
    effect = _effect({"collection": "users", "doc_id": "z"})
    _snap(adapter, effect)
    seen = {}

    def tool(database, collection, doc_id):
        seen["db"] = database
        seen["collection"] = collection
        seen["doc_id"] = doc_id

    adapter.apply(effect, tool)
    assert seen["db"] is db
    assert seen["collection"] == "users"
    assert seen["doc_id"] == "z"


def test_effect_touching_no_document_snapshots_nothing(adapter: MongoAdapter):
    effect = _effect({"unrelated": "value"})
    handle = _snap(adapter, effect)
    assert handle.payload["docs"] == []
    adapter.restore(handle)  # clean no-op


# --- skip-gated real-server smoke test ---------------------------------------


@pytest.mark.skipif(
    not os.environ.get("PHERIX_TEST_MONGO_URI"),
    reason="set PHERIX_TEST_MONGO_URI to run against a real MongoDB server",
)
def test_round_trip_against_real_pymongo():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(os.environ["PHERIX_TEST_MONGO_URI"])
    db = client.get_database("pherix_test")
    db.rt.delete_many({})
    db.rt.insert_one({"_id": "r1", "v": "before"})

    adapter = MongoAdapter(db)
    effect = _effect({"collection": "rt", "doc_id": "r1"})
    effect.snapshot = adapter.snapshot(effect)

    def tool(database, collection, doc_id):
        database[collection].update_one(
            {"_id": doc_id}, {"$set": {"v": "after"}}
        )

    adapter.apply(effect, tool)
    assert db.rt.find_one({"_id": "r1"})["v"] == "after"
    adapter.restore(effect.snapshot)
    assert db.rt.find_one({"_id": "r1"})["v"] == "before"
    db.rt.delete_many({})
