"""Audit journal — append-only SQLite persistence of the effect journal (D5).

A separate SQLite database, two tables (``transactions`` + ``effects``). Args,
snapshot and result are stored as JSON; effect ``status`` is updated in place.
There are no deletes. The finer event-log grain is deferred to Slice 5 — for
Slice 1, an in-place status update is enough to tell the whole story.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from pherix.core.effects import Effect, strict_json_default
from pherix.core.transaction import Transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    txn_id     TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    txn_id     TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    effect_id  TEXT NOT NULL,
    tool       TEXT NOT NULL,
    resource   TEXT NOT NULL,
    reversible INTEGER NOT NULL,
    status     TEXT NOT NULL,
    args       TEXT NOT NULL,
    snapshot   TEXT,
    result     TEXT,
    ts         TEXT NOT NULL,
    PRIMARY KEY (txn_id, idx)
);
"""


def _dump(value: Any) -> str | None:
    """Strict JSON dump (raises on non-journal-able types).

    Shares :func:`strict_json_default` with :mod:`pherix.core.effects` so the
    audit row is consistent with the idempotency key — both support bytes,
    datetime, dataclass; both reject silent ``str()`` coercion. See the Slice 1
    review follow-up resolved here.
    """
    if value is None:
        return None
    return json.dumps(value, default=strict_json_default, sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditJournal:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditJournal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transactions ---

    def record_transaction(self, txn: Transaction) -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO transactions (txn_id, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (txn.txn_id, txn.state.name, now, now),
        )
        self._conn.commit()

    def update_transaction_state(self, txn_id: str, state: str) -> None:
        self._conn.execute(
            "UPDATE transactions SET state = ?, updated_at = ? WHERE txn_id = ?",
            (state, _now(), txn_id),
        )
        self._conn.commit()

    def get_transaction(self, txn_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return dict(row) if row else None

    # --- effects ---

    def record_effect(self, effect: Effect) -> None:
        self._conn.execute(
            "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, "
            "reversible, status, args, snapshot, result, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                effect.txn_id,
                effect.index,
                effect.effect_id,
                effect.tool,
                effect.resource,
                int(effect.reversible),
                effect.status.name,
                _dump(effect.args),
                _dump(effect.snapshot),
                _dump(effect.result),
                effect.ts.isoformat(),
            ),
        )
        self._conn.commit()

    def update_effect(self, effect: Effect) -> None:
        """Update status / snapshot / result in place — same row, no history (D5)."""
        self._conn.execute(
            "UPDATE effects SET status = ?, snapshot = ?, result = ? "
            "WHERE txn_id = ? AND idx = ?",
            (
                effect.status.name,
                _dump(effect.snapshot),
                _dump(effect.result),
                effect.txn_id,
                effect.index,
            ),
        )
        self._conn.commit()

    def get_effects(self, txn_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        ).fetchall()
        return [dict(r) for r in rows]
