import sqlite3
import threading

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TransactionStateError, TxnState


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def _names(conn):
    return [r[0] for r in conn.execute("SELECT name FROM users ORDER BY id")]


@pytest.fixture
def insert_user():
    @tool(resource="sql")
    def insert_user(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    return insert_user


def test_reversible_write_auto_commits_on_clean_exit(conn, insert_user):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
        insert_user(name="alice")
    assert _names(conn) == ["bob", "alice"]
    assert txn.txn.state is TxnState.COMMITTED


def test_explicit_rollback_undoes_writes(conn, insert_user):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
        insert_user(name="alice")
        txn.rollback()
    assert _names(conn) == []
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_exception_in_block_auto_rolls_back(conn, insert_user):
    with pytest.raises(RuntimeError, match="agent failed"):
        with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
            insert_user(name="bob")
            raise RuntimeError("agent failed")
    assert _names(conn) == []
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_apply_failure_mid_transaction_rolls_back_cleanly(conn, insert_user):
    @tool(resource="sql")
    def boom(conn, name):
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        raise RuntimeError("tool exploded")

    audit = AuditJournal()
    with pytest.raises(RuntimeError, match="tool exploded"):
        with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
            insert_user(name="a")
            insert_user(name="b")
            boom(name="c")

    # snapshot precedes apply, so even the failed effect's partial write unwinds
    assert _names(conn) == []
    statuses = [e["status"] for e in audit.get_effects(txn.txn_id)]
    assert statuses == ["COMPENSATED", "COMPENSATED", "FAILED"]


def test_double_commit_raises(conn, insert_user):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
        txn.commit()
        with pytest.raises(TransactionStateError):
            txn.commit()


def test_double_rollback_raises(conn, insert_user):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
        txn.rollback()
        with pytest.raises(TransactionStateError):
            txn.rollback()


def test_commit_after_rollback_raises(conn, insert_user):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        txn.rollback()
        with pytest.raises(TransactionStateError):
            txn.commit()


def test_policy_denial_leaves_no_trace(conn, insert_user):
    audit = AuditJournal()
    with pytest.raises(PolicyViolation):
        with agent_txn(
            {"sql": SQLiteAdapter(conn)}, policy=Policy(allow=set()), audit=audit
        ) as txn:
            insert_user(name="bob")

    assert _names(conn) == []                       # zero DB effect
    assert txn.txn.effects == []                    # nothing appended to journal
    assert audit.get_effects(txn.txn_id) == []      # nothing in the audit log
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_empty_transaction_commits(conn):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        pass
    assert txn.txn.state is TxnState.COMMITTED
    assert txn.txn.effects == []


def test_journal_records_the_whole_story(conn, insert_user):
    audit = AuditJournal()
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
        insert_user(name="bob")
        insert_user(name="alice")

    record = audit.get_transaction(txn.txn_id)
    assert record["state"] == "COMMITTED"
    effects = audit.get_effects(txn.txn_id)
    assert [e["status"] for e in effects] == ["APPLIED", "APPLIED"]
    assert [e["tool"] for e in effects] == ["insert_user", "insert_user"]
    assert effects[0]["idx"] == 0 and effects[1]["idx"] == 1


def test_rollback_compensates_effects_newest_first(conn, insert_user):
    audit = AuditJournal()
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
        insert_user(name="a")
        insert_user(name="b")
        insert_user(name="c")
        assert _names(conn) == ["a", "b", "c"]
        txn.rollback()

    assert _names(conn) == []
    for effect in txn.txn.effects:
        assert effect.status is EffectStatus.COMPENSATED
    assert [e["status"] for e in audit.get_effects(txn.txn_id)] == [
        "COMPENSATED",
        "COMPENSATED",
        "COMPENSATED",
    ]


def test_effect_reversibility_comes_from_the_adapter(conn, insert_user):
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
    assert txn.txn.effects[0].reversible is True  # SQLiteAdapter.supports_rollback()


def test_cross_thread_use_raises_loudly(conn, insert_user):
    errors = []

    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:

        def worker():
            try:
                txn.record_tool_call("insert_user", (), {"name": "x"})
            except RuntimeError as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert len(errors) == 1
    assert "different thread" in str(errors[0])


def test_tool_called_outside_txn_runs_raw(conn, insert_user):
    insert_user(conn, name="solo")  # no agent_txn — transparent passthrough
    assert _names(conn) == ["solo"]


def test_unknown_resource_raises(conn):
    @tool(resource="http", injects_handle=False)
    def post_webhook(url):
        return url

    with pytest.raises(RuntimeError, match="no adapter"):
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            post_webhook(url="https://example.com")
