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
    txn_id        TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    replayed_from TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0
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
    read_keys  TEXT NOT NULL DEFAULT '[]',
    write_keys TEXT NOT NULL DEFAULT '[]',
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
    """SQLite-backed audit journal — persistent transcript of every effect.

    Slice 1 / P1 follow-up: ``path`` is required. Pherix is honest about its
    durability claim — the operator picks where journal persistence lives, no
    silent ``:memory:`` default. For tests and ephemeral runs that genuinely
    don't need persistence, call :meth:`in_memory` explicitly so the choice
    is visible at the call site rather than hiding in a default argument.
    """

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def in_memory(cls) -> "AuditJournal":
        """Construct an in-memory (non-durable) journal.

        Suitable for tests and one-off interactive use. Production callers
        should pass an explicit on-disk path to :meth:`__init__` so the
        journal survives process restart — the Slice 5 replay machinery
        depends on durable journals.
        """
        return cls(":memory:")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditJournal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transactions ---

    def record_transaction(
        self, txn: Transaction, *, dry_run: bool = False
    ) -> None:
        """Insert the per-transaction audit row.

        Slice 7 adds ``dry_run`` as a keyword-only flag (default ``False``)
        — passed from :class:`pherix.core.runtime.TxnContext` when the
        operator entered via :func:`pherix.dry_run`. The column lives on
        ``transactions`` so operators can filter dry-runs out of
        compliance views with a plain ``WHERE dry_run = 0``.
        """
        now = _now()
        self._conn.execute(
            "INSERT INTO transactions "
            "(txn_id, state, created_at, updated_at, replayed_from, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                txn.txn_id,
                txn.state.name,
                now,
                now,
                txn.replayed_from,
                int(dry_run),
            ),
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
            "reversible, status, args, snapshot, result, read_keys, write_keys, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                _dump(effect.read_keys) or "[]",
                _dump(effect.write_keys) or "[]",
                effect.ts.isoformat(),
            ),
        )
        self._conn.commit()

    def update_effect(self, effect: Effect) -> None:
        """Update mutable state in place — same row, no history (D5).

        Mutable fields: ``status``, ``snapshot``, ``result``, plus the
        isolation key triples ``read_keys`` / ``write_keys`` (which the
        resource handle / ``execute_isolated`` appends to during the
        adapter's ``apply``, AFTER the initial ``record_effect``). Slice 5
        replay reads these from the journal to verify isolation behaviour
        replays correctly.
        """
        self._conn.execute(
            "UPDATE effects SET status = ?, snapshot = ?, result = ?, "
            "read_keys = ?, write_keys = ? "
            "WHERE txn_id = ? AND idx = ?",
            (
                effect.status.name,
                _dump(effect.snapshot),
                _dump(effect.result),
                _dump(effect.read_keys) or "[]",
                _dump(effect.write_keys) or "[]",
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
