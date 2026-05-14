import pytest

from pherix.core.effects import Effect
from pherix.core.transaction import (
    Transaction,
    TransactionStateError,
    TxnState,
    new_txn_id,
)


def make_effect(txn_id, index):
    return Effect(
        txn_id=txn_id,
        index=index,
        tool="insert_user",
        args={"name": "bob"},
        resource="sql",
        reversible=True,
    )


def test_txn_state_enum_is_fully_defined():
    names = {s.name for s in TxnState}
    assert names == {"OPEN", "STAGED", "COMMITTED", "ROLLED_BACK", "PARTIAL", "STUCK"}


def test_new_transaction_defaults():
    txn = Transaction()
    assert txn.state is TxnState.OPEN
    assert txn.effects == []
    assert txn.is_open
    assert txn.txn_id


def test_txn_ids_are_unique():
    assert new_txn_id() != new_txn_id()
    assert Transaction().txn_id != Transaction().txn_id


def test_add_effect_appends_to_journal():
    txn = Transaction()
    e0 = make_effect(txn.txn_id, 0)
    e1 = make_effect(txn.txn_id, 1)
    txn.add_effect(e0)
    txn.add_effect(e1)
    assert txn.effects == [e0, e1]


def test_next_index_tracks_journal_length():
    txn = Transaction()
    assert txn.next_index() == 0
    txn.add_effect(make_effect(txn.txn_id, 0))
    assert txn.next_index() == 1


def test_commit_transition_allowed_from_open():
    txn = Transaction()
    txn.transition(TxnState.COMMITTED)
    assert txn.state is TxnState.COMMITTED


def test_rollback_transition_allowed_from_open():
    txn = Transaction()
    txn.transition(TxnState.ROLLED_BACK)
    assert txn.state is TxnState.ROLLED_BACK


def test_double_commit_raises():
    txn = Transaction()
    txn.transition(TxnState.COMMITTED)
    with pytest.raises(TransactionStateError):
        txn.transition(TxnState.COMMITTED)


def test_double_rollback_raises():
    txn = Transaction()
    txn.transition(TxnState.ROLLED_BACK)
    with pytest.raises(TransactionStateError):
        txn.transition(TxnState.ROLLED_BACK)


def test_commit_after_rollback_raises():
    txn = Transaction()
    txn.transition(TxnState.ROLLED_BACK)
    with pytest.raises(TransactionStateError):
        txn.transition(TxnState.COMMITTED)


def test_add_effect_after_close_raises():
    txn = Transaction()
    txn.transition(TxnState.COMMITTED)
    with pytest.raises(TransactionStateError):
        txn.add_effect(make_effect(txn.txn_id, 0))
