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


def test_explicit_commit_then_clean_exit_does_not_double_commit(conn, insert_user):
    # __exit__ must see the txn is already terminal and no-op — not call
    # commit() again and trip the double-commit guard.
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
        txn.commit()
    assert txn.txn.state is TxnState.COMMITTED
    assert _names(conn) == ["bob"]


def test_default_audit_persists_the_whole_story(conn, insert_user):
    # audit=None means "default audit store", not "no persistence".
    with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
        insert_user(name="bob")
        insert_user(name="alice")

    assert txn.audit is not None
    assert txn.audit.get_transaction(txn.txn_id)["state"] == "COMMITTED"
    assert [e["status"] for e in txn.audit.get_effects(txn.txn_id)] == [
        "APPLIED",
        "APPLIED",
    ]


def test_unknown_resource_raises(conn):
    @tool(resource="http", injects_handle=False)
    def post_webhook(url):
        return url

    with pytest.raises(RuntimeError, match="no adapter"):
        with agent_txn({"sql": SQLiteAdapter(conn)}):
            post_webhook(url="https://example.com")


def test_non_transactional_adapter_does_not_receive_lifecycle_calls():
    # D1 regression pin: lifecycle dispatch is by isinstance against the
    # TransactionalResourceAdapter sub-protocol, not by hasattr. A custom
    # adapter that only implements snapshot/apply/restore/supports_rollback
    # must NOT be auto-driven through begin/commit/rollback — that taxonomy
    # is reserved for adapters that explicitly opt into the sub-protocol.
    from pherix.core.adapters.base import SnapshotHandle

    calls: list[str] = []

    class _NonTxnAdapter:
        name = "memo"

        def supports_rollback(self) -> bool:
            return True

        def snapshot(self, effect):
            calls.append(f"snapshot:{effect.index}")
            return SnapshotHandle(resource=self.name, effect_index=effect.index)

        def apply(self, effect, tool_fn):
            calls.append(f"apply:{effect.index}")
            return tool_fn(**effect.args)

        def restore(self, handle):
            calls.append(f"restore:{handle.effect_index}")

        # Looks like a lifecycle hook by name but the adapter does NOT
        # conform to TransactionalResourceAdapter (no commit/rollback) —
        # so the runtime must not invoke it.
        def begin(self):
            calls.append("LIFECYCLE-LEAK-begin")

    @tool(resource="memo", injects_handle=False)
    def remember(value):
        return value

    with agent_txn({"memo": _NonTxnAdapter()}):
        remember(value="a")
        remember(value="b")

    assert "LIFECYCLE-LEAK-begin" not in calls
    assert calls == ["snapshot:0", "apply:0", "snapshot:1", "apply:1"]
