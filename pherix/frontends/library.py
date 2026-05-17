"""The Pherix library API surface — what wrapping an agent's tool layer needs.

``frontends/`` stays thin; ``core/`` knows nothing about how it is driven. That
is what lets the MCP gateway front-end (Slice 8) bolt onto the same core with no
rewrite.

Slice 1 exposes the reversible path only. ``approve_irreversible`` arrives with
the staging / gating machinery in Slice 3 — there is no irreversible effect to
approve yet, so it is deliberately not exported here.
"""

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
)
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool

__all__ = [
    "agent_txn",
    "tool",
    "Policy",
    "PolicyViolation",
    "SQLiteAdapter",
    "FilesystemAdapter",
    "AuditJournal",
    "ResourceAdapter",
    "TransactionalResourceAdapter",
    "SnapshotHandle",
]
