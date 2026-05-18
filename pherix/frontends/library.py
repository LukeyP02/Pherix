"""The Pherix library API surface — what wrapping an agent's tool layer needs.

``frontends/`` stays thin; ``core/`` knows nothing about how it is driven. That
is what lets the MCP gateway front-end (Slice 8) bolt onto the same core with no
rewrite.

Slice 3 adds the irreversible lane: ``HTTPAdapter`` (the honest "I cannot
roll back" adapter), ``StagedResult`` (the sentinel agents receive from
staged tool calls), and the gate-related errors ``GateBlocked`` /
``CompensatorNotRegistered``. ``approve_irreversible`` lives on the
``TxnContext`` yielded by :func:`agent_txn`, not as a standalone function:
approval is *per-transaction*, not global.
"""

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
)
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter, IrreversibleAdapterError
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import StagedResult
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import CompensatorNotRegistered, GateBlocked, agent_txn
from pherix.core.tools import tool

__all__ = [
    "agent_txn",
    "tool",
    "Policy",
    "PolicyViolation",
    "SQLiteAdapter",
    "FilesystemAdapter",
    "HTTPAdapter",
    "AuditJournal",
    "ResourceAdapter",
    "TransactionalResourceAdapter",
    "SnapshotHandle",
    "StagedResult",
    "GateBlocked",
    "CompensatorNotRegistered",
    "IrreversibleAdapterError",
]
