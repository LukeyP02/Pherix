import json

import pytest

from pherix.core.adapters.base import SnapshotHandle
from pathlib import Path

from pherix.core.audit import AuditJournal, default_journal_path
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState

# Trust pillar: audit — the journal records every effect (and round-trips
# isolation keys / verdicts) durably.
pytestmark = pytest.mark.audit


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
    j = AuditJournal.in_memory()
    txn = Transaction()
    j.record_transaction(txn)
    row = j.get_transaction(txn.txn_id)
    assert row["state"] == "OPEN"
    assert row["created_at"] == row["updated_at"]


def test_update_transaction_state():
    j = AuditJournal.in_memory()
    txn = Transaction()
    j.record_transaction(txn)
    j.update_transaction_state(txn.txn_id, TxnState.COMMITTED.name)
    assert j.get_transaction(txn.txn_id)["state"] == "COMMITTED"


def test_record_effect_persists_json_args():
    j = AuditJournal.in_memory()
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
    j = AuditJournal.in_memory()
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
    j = AuditJournal.in_memory()
    txn = Transaction()
    j.record_transaction(txn)
    for i, name in enumerate(["a", "b", "c"]):
        j.record_effect(make_effect(txn.txn_id, index=i, args={"name": name}))
    rows = j.get_effects(txn.txn_id)
    assert [r["idx"] for r in rows] == [0, 1, 2]
    assert [json.loads(r["args"])["name"] for r in rows] == ["a", "b", "c"]


def test_get_unknown_transaction_returns_none():
    assert AuditJournal.in_memory().get_transaction("nope") is None


# --- Slice 1 review P2: audit row persists read_keys / write_keys ----------


def test_audit_persists_read_keys_and_write_keys_round_trip():
    """Slice 4 isolation triples must survive the journal round-trip — Slice 5
    replay reads them back to verify isolation behaviour replays correctly.
    """
    j = AuditJournal.in_memory()
    txn = Transaction()
    j.record_transaction(txn)
    e = make_effect(
        txn.txn_id,
        read_keys=[("sql", ("users", "alice"), 5), ("fs", ("/notes/a.txt",), "sha256:abc")],
        write_keys=[("sql", ("users", "alice")), ("fs", ("/notes/a.txt",))],
    )
    j.record_effect(e)

    rows = j.get_effects(txn.txn_id)
    assert len(rows) == 1
    # JSON round-trip — keys are tuples in-memory, lists in the audit row.
    rk = json.loads(rows[0]["read_keys"])
    wk = json.loads(rows[0]["write_keys"])
    assert rk == [["sql", ["users", "alice"], 5], ["fs", ["/notes/a.txt"], "sha256:abc"]]
    assert wk == [["sql", ["users", "alice"]], ["fs", ["/notes/a.txt"]]]


def test_audit_default_empty_read_write_keys_serialise_as_empty_list():
    """An effect with no isolation involvement journalls as empty lists,
    not NULL — keeps the schema invariant that read_keys / write_keys
    are always JSON-parseable arrays.
    """
    j = AuditJournal.in_memory()
    txn = Transaction()
    j.record_transaction(txn)
    j.record_effect(make_effect(txn.txn_id))
    rows = j.get_effects(txn.txn_id)
    assert json.loads(rows[0]["read_keys"]) == []
    assert json.loads(rows[0]["write_keys"]) == []


def test_audit_update_effect_persists_late_appended_keys():
    """read_keys / write_keys are appended DURING adapter.apply, AFTER the
    initial record_effect. update_effect must persist the now-populated lists.
    """
    j = AuditJournal.in_memory()
    txn = Transaction()
    j.record_transaction(txn)
    e = make_effect(txn.txn_id)
    j.record_effect(e)  # empty read/write keys

    # Simulate the runtime: handle appends during apply.
    e.read_keys.append(("sql", ("users", "bob"), 7))
    e.write_keys.append(("sql", ("users", "bob")))
    e.status = EffectStatus.APPLIED
    j.update_effect(e)

    rows = j.get_effects(txn.txn_id)
    assert json.loads(rows[0]["read_keys"]) == [["sql", ["users", "bob"], 7]]
    assert json.loads(rows[0]["write_keys"]) == [["sql", ["users", "bob"]]]
    assert rows[0]["status"] == "APPLIED"


# --- the durable default location (flight-recorder: persist by default) ------


def test_default_journal_path_honours_env_var(monkeypatch, tmp_path):
    """$PHERIX_JOURNAL, when set, names the journal location verbatim."""
    target = str(tmp_path / "custom" / "journal.db")
    monkeypatch.setenv("PHERIX_JOURNAL", target)
    assert default_journal_path() == target


def test_default_journal_path_falls_back_to_home(monkeypatch, tmp_path):
    """With $PHERIX_JOURNAL unset, the default is ~/.pherix/journal.db, and the
    parent ~/.pherix/ directory is created on the way out."""
    monkeypatch.delenv("PHERIX_JOURNAL", raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    path = default_journal_path()

    assert path == str(fake_home / ".pherix" / "journal.db")
    assert (fake_home / ".pherix").is_dir()


def test_audit_default_opens_the_canonical_path(monkeypatch, tmp_path):
    """AuditJournal.default() is the persistent default — it opens the file at
    default_journal_path(), NOT an in-memory journal."""
    target = str(tmp_path / "journal.db")
    monkeypatch.setenv("PHERIX_JOURNAL", target)
    with AuditJournal.default() as j:
        assert j.path == target
        assert j.path != ":memory:"
    assert Path(target).exists()


def test_in_memory_remains_the_explicit_ephemeral_opt_out():
    """in_memory() still yields a non-durable :memory: journal — the explicit
    way to opt OUT of persistence now that default() persists."""
    assert AuditJournal.in_memory().path == ":memory:"
