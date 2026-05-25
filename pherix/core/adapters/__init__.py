"""Resource adapters — the per-backend ``snapshot/apply/restore`` triples.

Each adapter lazy-imports its driver inside its own methods, never at module
load, so ``import pherix`` is dependency-free. ``git`` and ``memory`` adapters
are imported directly from their modules by their users (not re-exported here).
"""

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    StateDiffable,
    TransactionalResourceAdapter,
    VersionedResourceAdapter,
)
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter, IrreversibleAdapterError
from pherix.core.adapters.sql import SQLiteAdapter

__all__ = [
    # protocols
    "ResourceAdapter",
    "TransactionalResourceAdapter",
    "VersionedResourceAdapter",
    "StateDiffable",
    "SnapshotHandle",
    # reversible (snapshot / savepoint lane)
    "SQLiteAdapter",
    "FilesystemAdapter",
    # irreversible (staged / compensated lane)
    "HTTPAdapter",
    "IrreversibleAdapterError",
]
