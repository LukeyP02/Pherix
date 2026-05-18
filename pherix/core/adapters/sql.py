"""SQLiteAdapter — the reversible adapter, correct by construction.

The database does the heavy lifting: ``snapshot`` issues a real ``SAVEPOINT``,
``restore`` does ``ROLLBACK TO SAVEPOINT``. The connection must be opened in
autocommit mode (``isolation_level=None``) so this adapter — not sqlite3's
implicit machinery — controls every BEGIN / SAVEPOINT / COMMIT / ROLLBACK.

This module never imports ``core/tools.py``: the runtime resolves ``effect.tool``
to a callable and passes it to ``apply`` as ``tool_fn``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# Side-table holding monotonic version counters per (resource, key).
# ``key_json`` is ``json.dumps(list(key), sort_keys=True)`` so cross-process
# readers (multiple Python processes against the same SQLite file) see
# consistent rows. The table is created on adapter init.
_VERSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _pherix_versions (
    resource TEXT NOT NULL,
    key_json TEXT NOT NULL,
    version  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, key_json)
)
"""


class SQLiteAdapter:
    name = "sql"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        # Slice 4: create the version side-table eagerly so the first
        # read_version on an unknown key returns 0 (not a missing-table
        # error). DDL is idempotent so re-binding the adapter is safe.
        self._conn.execute(_VERSIONS_TABLE_DDL)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (driven by the runtime, D3) ---

    def begin(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # --- per-effect snapshot / apply / restore ---

    @staticmethod
    def _savepoint_name(index: int) -> str:
        # SQLite cannot parameterise identifiers. `index` is an internal,
        # runtime-assigned integer (never user input), so building the
        # savepoint name by interpolation is safe by construction.
        return f"sp_{int(index)}"

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        sp = self._savepoint_name(effect.index)
        self._conn.execute(f"SAVEPOINT {sp}")
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"savepoint": sp},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # D2: the txn-owned connection is injected as the tool's first arg;
        # the @tool wrapper hides it from the agent's call-site.
        return tool_fn(self._conn, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        sp = handle.payload["savepoint"]
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")

    # --- versioning (Slice 4 — VersionedResourceAdapter) -------------------

    @staticmethod
    def _encode_key(key: tuple) -> str:
        # Pre-coercing the tuple to a list gives a stable JSON form across
        # processes regardless of how json handles tuples. ``sort_keys`` is
        # a no-op for lists but keeps the encoding canonical if a key ever
        # contains a nested dict.
        return json.dumps(list(key), sort_keys=True)

    def read_version(self, key: tuple) -> int:
        row = self._conn.execute(
            "SELECT version FROM _pherix_versions "
            "WHERE resource = ? AND key_json = ?",
            (self.name, self._encode_key(key)),
        ).fetchone()
        # Absent row → version 0 ("never written"). Never returns None.
        return 0 if row is None else int(row[0])

    def write_version(self, key: tuple) -> int:
        # Atomic UPSERT with RETURNING so the bump is race-free against a
        # second connection to the same on-disk SQLite file.
        cur = self._conn.execute(
            "INSERT INTO _pherix_versions (resource, key_json, version) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(resource, key_json) DO UPDATE "
            "SET version = version + 1 "
            "RETURNING version",
            (self.name, self._encode_key(key)),
        )
        row = cur.fetchone()
        return int(row[0])
