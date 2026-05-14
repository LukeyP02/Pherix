import json

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState


def make_effect(txn_id, index=0, **overrides):
    base = dict(
        txn_id=txn_id,
        index=index,
        tool="insert_user",
        args={"name": "bob"},
        resource="sql",
        reversible=True,
    )
    base.update(overrides)
    return Effect(**base)


def test_record_and_get_transaction():
    j = AuditJournal()
    txn = Transaction()
    j.record_transaction(txn)
    row = j.get_transaction(txn.txn_id)
    assert row["state"] == "OPEN"
    assert row["created_at"] == row["updated_at"]


def test_update_transaction_state():
    j = AuditJournal()
    txn = Transaction()
    j.record_transaction(txn)
    j.update_transaction_state(txn.txn_id, TxnState.COMMITTED.name)
    assert j.get_transaction(txn.txn_id)["state"] == "COMMITTED"


def test_record_effect_persists_json_args():
    j = AuditJournal()
    txn = Transaction()
    j.record_transaction(txn)
    e = make_effect(txn.txn_id, args={"name": "bob", "role": "admin"})
    j.record_effect(e)
    rows = j.get_effects(txn.txn_id)
    assert len(rows) == 1
    assert json.loads(rows[0]["args"]) == {"name": "bob", "role": "admin"}
    assert rows[0]["status"] == "STAGED"
    assert rows[0]["reversible"] == 1


def test_update_effect_is_in_place_no_new_row():
    j = AuditJournal()
    txn = Transaction()
    j.record_transaction(txn)
    e = make_effect(txn.txn_id)
    j.record_effect(e)

    e.status = EffectStatus.APPLIED
    e.snapshot = SnapshotHandle(resource="sql", effect_index=0, payload={"savepoint": "sp_0"})
    e.result = 1
    j.update_effect(e)

    rows = j.get_effects(txn.txn_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "APPLIED"
    assert json.loads(row["snapshot"])["payload"] == {"savepoint": "sp_0"}
    assert json.loads(row["result"]) == 1


def test_journal_completeness_for_multi_effect_transaction():
    j = AuditJournal()
    txn = Transaction()
    j.record_transaction(txn)
    for i, name in enumerate(["a", "b", "c"]):
        j.record_effect(make_effect(txn.txn_id, index=i, args={"name": name}))
    rows = j.get_effects(txn.txn_id)
    assert [r["idx"] for r in rows] == [0, 1, 2]
    assert [json.loads(r["args"])["name"] for r in rows] == ["a", "b", "c"]


def test_get_unknown_transaction_returns_none():
    assert AuditJournal().get_transaction("nope") is None
