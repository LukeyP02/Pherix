"""SQLiteAdapter — the reversible adapter, correct by construction.

The database does the heavy lifting: ``snapshot`` issues a real ``SAVEPOINT``,
``restore`` does ``ROLLBACK TO SAVEPOINT``. The connection must be opened in
autocommit mode (``isolation_level=None``) so this adapter — not sqlite3's
implicit machinery — controls every BEGIN / SAVEPOINT / COMMIT / ROLLBACK.

This module never imports ``core/tools.py``: the runtime resolves ``effect.tool``
to a callable and passes it to ``apply`` as ``tool_fn``.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


class SQLiteAdapter:
    name = "sql"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

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
