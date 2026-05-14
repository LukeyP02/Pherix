"""Transaction: the state machine that owns the ordered effect journal.

Slice 1 exercises only ``OPEN -> COMMITTED`` and ``OPEN -> ROLLED_BACK``.
``STAGED`` / ``PARTIAL`` / ``STUCK`` are defined but unused until later slices.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from pherix.core.effects import Effect


class TxnState(Enum):
    OPEN = "open"
    STAGED = "staged"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    PARTIAL = "partial"
    STUCK = "stuck"


class TransactionStateError(RuntimeError):
    """Raised on an illegal transaction state transition or journal mutation."""


# Slice 1 only: the reversible commit / rollback paths.
_ALLOWED_TRANSITIONS: dict[TxnState, set[TxnState]] = {
    TxnState.OPEN: {TxnState.COMMITTED, TxnState.ROLLED_BACK},
}


def new_txn_id() -> str:
    return f"txn-{uuid.uuid4().hex[:12]}"


@dataclass
class Transaction:
    txn_id: str = field(default_factory=new_txn_id)
    state: TxnState = TxnState.OPEN
    effects: list[Effect] = field(default_factory=list)
    policy: object | None = None

    @property
    def is_open(self) -> bool:
        return self.state is TxnState.OPEN

    def next_index(self) -> int:
        """Index the next appended effect will occupy."""
        return len(self.effects)

    def add_effect(self, effect: Effect) -> None:
        if not self.is_open:
            raise TransactionStateError(
                f"cannot append to journal of transaction in state {self.state.name}"
            )
        self.effects.append(effect)

    def transition(self, to: TxnState) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(self.state, set())
        if to not in allowed:
            raise TransactionStateError(
                f"illegal transition {self.state.name} -> {to.name}"
            )
        self.state = to
