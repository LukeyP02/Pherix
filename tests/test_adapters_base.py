from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
)
from pherix.core.effects import Effect


def test_snapshot_handle_defaults():
    h = SnapshotHandle(resource="sql", effect_index=2)
    assert h.resource == "sql"
    assert h.effect_index == 2
    assert h.payload == {}


def test_snapshot_handle_carries_json_payload():
    h = SnapshotHandle(resource="sql", effect_index=0, payload={"savepoint": "sp_0"})
    assert h.payload == {"savepoint": "sp_0"}


class _ConformingAdapter:
    name = "fake"

    def supports_rollback(self) -> bool:
        return True

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        return SnapshotHandle(resource=self.name, effect_index=effect.index)

    def apply(self, effect, tool_fn):
        return tool_fn(**effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        pass


class _NotAnAdapter:
    name = "broken"

    def apply(self, effect, tool_fn):
        return None


def test_conforming_class_satisfies_protocol():
    assert isinstance(_ConformingAdapter(), ResourceAdapter)


def test_incomplete_class_fails_protocol():
    assert not isinstance(_NotAnAdapter(), ResourceAdapter)


class _TxnConformingAdapter(_ConformingAdapter):
    name = "txn-fake"

    def begin(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_txn_adapter_subprotocol_recognises_lifecycle():
    a = _TxnConformingAdapter()
    assert isinstance(a, ResourceAdapter)
    assert isinstance(a, TransactionalResourceAdapter)


def test_txn_adapter_subprotocol_rejects_lifecycle_less_adapter():
    # A plain ResourceAdapter (no begin/commit/rollback) must NOT satisfy the
    # transactional sub-protocol — that's the whole point of D1: the type
    # system reflects which adapters have a transaction-scope lifecycle.
    assert isinstance(_ConformingAdapter(), ResourceAdapter)
    assert not isinstance(_ConformingAdapter(), TransactionalResourceAdapter)


def test_sqlite_adapter_is_transactional():
    import sqlite3

    from pherix.core.adapters.sql import SQLiteAdapter

    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        assert isinstance(SQLiteAdapter(conn), TransactionalResourceAdapter)
    finally:
        conn.close()
