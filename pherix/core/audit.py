"""Audit journal — append-only SQLite persistence of the effect journal (D5).

A separate SQLite database, two tables (``transactions`` + ``effects``). Args,
snapshot and result are stored as JSON; effect ``status`` is updated in place.
There are no deletes. The finer event-log grain is deferred to Slice 5 — for
Slice 1, an in-place status update is enough to tell the whole story.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
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
    dry_run       INTEGER NOT NULL DEFAULT 0,
    client_id     TEXT
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
    actor      TEXT,
    ts         TEXT NOT NULL,
    PRIMARY KEY (txn_id, idx)
);
CREATE TABLE IF NOT EXISTS verdicts (
    txn_id       TEXT NOT NULL,
    effect_index INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    phase        TEXT NOT NULL,   -- 'stage' | 'commit'
    allow        INTEGER NOT NULL,
    kind         TEXT NOT NULL,   -- 'rule' | 'cap' | 'allowlist'
    rule_name    TEXT,
    reason       TEXT,
    PRIMARY KEY (txn_id, seq)
);
CREATE TABLE IF NOT EXISTS conflicts (
    txn_id            TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    resource          TEXT NOT NULL,
    key               TEXT NOT NULL,   -- JSON: the (resource-local) key tuple
    version_at_read   TEXT,            -- JSON: version observed at read time
    version_now       TEXT,            -- JSON: version the adapter reports at commit
    version_expected  TEXT,            -- JSON: what version_now was compared against
    ts                TEXT NOT NULL,
    PRIMARY KEY (txn_id, seq)
);
CREATE TABLE IF NOT EXISTS approvals (
    txn_id      TEXT NOT NULL,
    effect_id   TEXT NOT NULL,
    token       TEXT NOT NULL,   -- stable opaque handle, the over-the-wire key
    status      TEXT NOT NULL,   -- 'PENDING' | 'APPROVED'
    approver    TEXT,            -- the principal who approved (#40 actor model); NULL while PENDING
    requested_at TEXT NOT NULL,  -- when the gate persisted the PENDING record
    approved_at  TEXT,           -- when approve() recorded the APPROVED entry; NULL while PENDING
    PRIMARY KEY (txn_id, effect_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_token ON approvals (token);
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


def default_journal_path() -> str:
    """The canonical on-disk journal location for a default agent run.

    Returns ``$PHERIX_JOURNAL`` when the operator has named a location, else
    ``~/.pherix/journal.db``. The parent directory (``~/.pherix/``) is created
    if absent so the caller can open the connection without a pre-flight
    ``mkdir``. This *names* the durable default; it does not weaken the
    "operator picks durability" stance — :meth:`AuditJournal.in_memory` stays
    the explicit ephemeral opt-out.
    """
    env_path = os.environ.get("PHERIX_JOURNAL")
    if env_path:
        return env_path
    path = Path.home() / ".pherix" / "journal.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


class AuditJournal:
    """SQLite-backed audit journal — persistent transcript of every effect.

    Slice 1 / P1 follow-up: ``path`` is required. Pherix is honest about its
    durability claim — the operator picks where journal persistence lives, no
    silent ``:memory:`` default. For tests and ephemeral runs that genuinely
    don't need persistence, call :meth:`in_memory` explicitly so the choice
    is visible at the call site rather than hiding in a default argument.
    """

    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Additive migration: ``CREATE TABLE IF NOT EXISTS`` is a no-op against
        # an existing ``effects`` table, so a journal created before the
        # ``actor`` column was added does NOT gain it from the schema above.
        # Add it idempotently here, guarded by a column-exists check so a fresh
        # journal (already carrying the column) and an upgraded one converge on
        # the same shape. SQLite ``ADD COLUMN`` is cheap (metadata-only) and
        # the new column defaults to NULL for every existing row — so old
        # effects read back as ``actor = None``, never crash. See the
        # NULL-tolerance contract this column is held to.
        self._ensure_column("effects", "actor", "TEXT")
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Idempotently add ``column`` to ``table`` if it is not already there.

        Reads the live schema via ``PRAGMA table_info`` and only issues the
        ``ALTER TABLE ... ADD COLUMN`` when the column is absent — so the
        method is safe to call on every connect, against both a fresh journal
        (column present from the schema) and a pre-existing one (column added
        here). ``table`` / ``column`` / ``decl`` are code-supplied constants,
        never agent input, so interpolating them is safe (SQLite cannot
        parameterise DDL identifiers regardless).
        """
        existing = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
            )

    @property
    def path(self) -> str:
        """The journal's DB path (``":memory:"`` for an in-memory journal).

        The ship layer reads this to open its own thread-confined connection for
        background shipping — an in-memory journal has no shareable path, so
        backgrounding it is rejected at the call site.
        """
        return self._path

    @classmethod
    def default(cls) -> "AuditJournal":
        """Construct a journal at the canonical durable location.

        Delegates to :func:`default_journal_path` — ``$PHERIX_JOURNAL`` if set,
        else ``~/.pherix/journal.db``. This is what an :func:`agent_txn` with no
        explicit ``audit=`` opens, so default agent runs persist their journal
        across process restarts (the Slice 5 replay machinery depends on it).
        """
        return cls(default_journal_path())

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
        self, txn: Transaction, *, dry_run: bool = False, client_id: str | None = None
    ) -> None:
        """Insert the per-transaction audit row.

        Slice 7 adds ``dry_run`` as a keyword-only flag (default ``False``)
        — passed from :class:`pherix.core.runtime.TxnContext` when the
        operator entered via :func:`pherix.dry_run`. The column lives on
        ``transactions`` so operators can filter dry-runs out of
        compliance views with a plain ``WHERE dry_run = 0``.

        Slice 8 adds ``client_id`` as the third instance of the same
        additive pattern (``replayed_from``, ``dry_run``, ``client_id``): a
        nullable column, keyword-only param, default NULL. A gateway
        front-end serving many MCP clients through one core passes the
        calling client's identity so audit rows carry provenance; library
        callers never supply one and the column stays NULL.
        """
        now = _now()
        self._conn.execute(
            "INSERT INTO transactions "
            "(txn_id, state, created_at, updated_at, replayed_from, dry_run, "
            "client_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                txn.txn_id,
                txn.state.name,
                now,
                now,
                txn.replayed_from,
                int(dry_run),
                client_id,
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
        # ``actor`` (the on-whose-authority principal) is set at Effect
        # construction and never mutates, so it is written here on insert and
        # left untouched by :meth:`update_effect`. NULL for any effect with no
        # declared actor — the column is nullable and the reader degrades to
        # ``None``.
        self._conn.execute(
            "INSERT INTO effects (txn_id, idx, effect_id, tool, resource, "
            "reversible, status, args, snapshot, result, read_keys, write_keys, "
            "actor, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                effect.actor,
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

    # --- policy verdicts (D3 — the recorded, not generated, decision) ---

    def record_verdicts(self, txn_id: str, rows: list[dict]) -> None:
        """Persist per-rule policy verdicts for a transaction.

        Each row is a plain dict — ``effect_index``, ``phase``
        (``'stage'`` / ``'commit'``), ``allow`` (bool), ``kind``
        (``'rule'`` / ``'cap'`` / ``'allowlist'``), ``rule_name`` and
        ``reason`` — so this method stays decoupled from the policy module's
        :class:`~pherix.core.policy.PolicyVerdict` type. ``seq`` preserves
        order within the transaction (the list order as evaluated). Best-
        effort and append-only like the rest of the journal: the verdict
        record annotates the transaction, it is never the source of truth
        for whether an effect took place — that is the effect ``status``.

        The verdict surface is currently populated on the dry-run path
        (where the runtime captures every rule's decision without raising);
        normal-commit verdict capture is a clean additive follow-up that
        writes here too.
        """
        for seq, r in enumerate(rows):
            self._conn.execute(
                "INSERT INTO verdicts (txn_id, effect_index, seq, phase, "
                "allow, kind, rule_name, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    txn_id,
                    int(r["effect_index"]),
                    seq,
                    r["phase"],
                    int(bool(r["allow"])),
                    r.get("kind", "rule"),
                    r.get("rule_name"),
                    r.get("reason"),
                ),
            )
        self._conn.commit()

    def get_verdicts(self, txn_id: str) -> list[dict]:
        # seq is global insertion order (stage verdicts recorded before commit
        # verdicts), so ordering by (effect_index, seq) yields, per effect, its
        # stage decisions then its commit decisions — temporal order.
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE txn_id = ? "
            "ORDER BY effect_index, seq",
            (txn_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- isolation conflicts (Prong #2 — the recorded non-commutativity) ----

    def record_conflicts(self, txn_id: str, conflicts: list) -> None:
        """Persist the commit-time isolation conflicts for a transaction.

        A conflict is the non-commutativity event the diff finds at commit:
        another transaction committed a write to a key this txn read between
        its read and its commit. Until now it lived only as a raised
        :class:`~pherix.core.isolation.IsolationConflict` — the journal went
        silent on it, so conflicts were uncountable. This makes a conflict a
        first-class journal record, same append-only shape as ``verdicts``.

        Each item is a :class:`~pherix.core.isolation.Conflict` dataclass (the
        runtime passes the diff's output straight through), but this method
        only reads attribute names (``resource``, ``key``, ``version_at_read``,
        ``version_now``, ``version_expected``) so it stays decoupled from the
        isolation module's type — a plain object with those attributes works
        too. ``seq`` preserves the diff's emission order. Versions are stored
        as JSON because a version can be an int (SQLite counter) or any
        JSON-able opaque token an adapter chooses.

        Append-only and best-effort like the rest of the journal: this records
        that a conflict was *detected*; it never decides the txn's fate. The
        resolution policy (Abort / Retry / Serialize) does that — and this is
        written BEFORE the policy runs so the record survives both the raise
        (Abort / Serialize) and the rollback-and-replay (Retry).
        """
        now = _now()
        for seq, c in enumerate(conflicts):
            self._conn.execute(
                "INSERT INTO conflicts (txn_id, seq, resource, key, "
                "version_at_read, version_now, version_expected, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    txn_id,
                    seq,
                    c.resource,
                    _dump(list(c.key)) or "[]",
                    _dump(c.version_at_read),
                    _dump(c.version_now),
                    _dump(getattr(c, "version_expected", None)),
                    now,
                ),
            )
        self._conn.commit()

    def get_conflicts(self, txn_id: str) -> list[dict]:
        """Recorded isolation conflicts for ``txn_id``, in diff-emission order."""
        rows = self._conn.execute(
            "SELECT * FROM conflicts WHERE txn_id = ? ORDER BY seq",
            (txn_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- approvals (over-the-wire human gate) -------------------------------
    #
    # The in-process gate (``approve_irreversible``) lives only in process
    # memory: the human who clears it must hold the live ``TxnContext``. To
    # approve from OUTSIDE the agent's process — a reviewer, a higher-trust
    # service — the approval has to be a *journal record*, the same append-only
    # substrate everything else folds over. A gated commit persists a PENDING
    # row keyed by ``(txn_id, effect_id)`` carrying a stable ``token``; an
    # out-of-process ``approve(token)`` flips that row to APPROVED and stamps
    # the approver; a resumed ``commit()`` reads the APPROVED rows and lets the
    # gate pass. The token is the only thing the approver needs — it never has
    # to reconstruct the txn or its effects.

    def record_pending_approval(
        self, txn_id: str, effect_id: str, token: str
    ) -> None:
        """Persist (or refresh) a PENDING approval request for one staged effect.

        Idempotent on ``(txn_id, effect_id)``: a re-gated commit (the operator
        ran the body again, or a second resume attempt) refreshes the
        ``requested_at`` timestamp and reasserts PENDING, but only if the row
        is not *already* APPROVED — an approval that has landed out-of-process
        between two gate evaluations must NOT be clobbered back to PENDING.
        The ON CONFLICT clause is the whole TOCTOU-safety of the write: it is
        the journal equivalent of compare-and-set on the approval status.
        """
        now = _now()
        self._conn.execute(
            "INSERT INTO approvals "
            "(txn_id, effect_id, token, status, approver, requested_at, "
            "approved_at) "
            "VALUES (?, ?, ?, 'PENDING', NULL, ?, NULL) "
            "ON CONFLICT(txn_id, effect_id) DO UPDATE SET "
            "requested_at = excluded.requested_at "
            "WHERE approvals.status != 'APPROVED'",
            (txn_id, effect_id, token, now),
        )
        self._conn.commit()

    def record_approval(self, token: str, approver: str | None) -> dict:
        """Flip a PENDING approval to APPROVED, stamping the approver (#40 actor).

        This is the over-the-wire write — called by the proxy/MCP gateway's
        ``approve`` operation, in a *different* process from the one holding
        the transaction. Resolves the opaque ``token`` to its
        ``(txn_id, effect_id)``, records ``approver`` (the on-whose-authority
        principal) and ``approved_at``, and returns the updated row so the
        caller can echo what was approved.

        Idempotent: approving an already-APPROVED token is a no-op that returns
        the existing row (the first approver/timestamp stand — re-approval
        never rewrites who authorised it). An unknown token raises
        :class:`KeyError` — a typo must not silently succeed and let a gate
        pass on an approval that was never recorded.
        """
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            raise KeyError(
                f"no pending approval for token {token!r} — the token is "
                f"unknown or the gate never persisted it"
            )
        if row["status"] != "APPROVED":
            self._conn.execute(
                "UPDATE approvals SET status = 'APPROVED', approver = ?, "
                "approved_at = ? WHERE token = ? AND status != 'APPROVED'",
                (approver, _now(), token),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE token = ?", (token,)
            ).fetchone()
        return dict(row)

    def get_approvals(self, txn_id: str) -> list[dict]:
        """All approval records for ``txn_id`` — PENDING and APPROVED alike.

        Ordered by ``requested_at`` then ``effect_id`` for a stable read. The
        resumed commit folds this list to learn which staged effects have an
        APPROVED record waiting; the inspector reads it (read-only) to render
        who is waiting on whom.
        """
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE txn_id = ? "
            "ORDER BY requested_at, effect_id",
            (txn_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def approved_effect_ids(self, txn_id: str) -> set[str]:
        """The set of ``effect_id``s for ``txn_id`` with an APPROVED record.

        The exact shape the commit-time gate needs: it unions this with the
        in-process pre-approvals before deciding whether any staged effect
        still needs approval. A pure read of the journal — the resume path is,
        like everything else, a fold over the persisted log.
        """
        rows = self._conn.execute(
            "SELECT effect_id FROM approvals "
            "WHERE txn_id = ? AND status = 'APPROVED'",
            (txn_id,),
        ).fetchall()
        return {r["effect_id"] for r in rows}

    # --- shipping (forward-read the journal past a cursor) ------------------

    # The journalled tables, in dependency order (a txn row before its
    # effects, effects before their verdicts / conflicts) so a control plane
    # that validates foreign keys never sees an orphan.
    _SHIPPABLE_TABLES = (
        "transactions", "effects", "verdicts", "conflicts", "approvals"
    )

    def export_since(self, cursor: dict | None) -> tuple[dict, dict]:
        """Read journal rows newer than ``cursor`` for shipping to the control plane.

        Shipping is just the journal read **forward** past a high-water mark —
        the same fold the rest of the engine is built on, here truncated to "give
        me what I have not sent yet". ``cursor`` is a ``{table: last_rowid}`` map;
        the returned ``new_cursor`` advances each table to the largest rowid read.
        SQLite's implicit ``rowid`` is a monotonic append counter per table, so
        ``rowid > last`` is exactly "rows appended since I last looked".

        Append-only + idempotent ingest means a coarse cursor is safe: if a ship
        succeeds but the cursor advance is lost (a crash between the two), the
        rows simply re-ship and the control plane skips them on primary key.

        Returns ``(rows_by_table, new_cursor)``. Each row is a plain dict
        including its ``rowid``; payload encryption/redaction is the shipper's
        job, not the journal's — the journal hands over cleartext rows and the
        ship layer is the trust boundary.
        """
        cursor = cursor or {}
        rows_by_table: dict[str, list[dict]] = {}
        new_cursor = dict(cursor)
        for table in self._SHIPPABLE_TABLES:
            after = int(cursor.get(table, 0))
            rows = self._conn.execute(
                f"SELECT rowid AS rowid, * FROM {table} "
                f"WHERE rowid > ? ORDER BY rowid",
                (after,),
            ).fetchall()
            rows_by_table[table] = [dict(r) for r in rows]
            if rows:
                new_cursor[table] = int(rows[-1]["rowid"])
        return rows_by_table, new_cursor

    _SHIP_CURSOR_DDL = (
        "CREATE TABLE IF NOT EXISTS _pherix_ship_cursor ("
        "table_name TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL)"
    )

    def get_ship_cursor(self) -> dict:
        """The durable ``{table: last_rowid}`` high-water the shipper has sent.

        Persisted in the journal DB so a process restart resumes where it left
        off rather than re-shipping the whole journal. Empty on first use.
        """
        self._conn.execute(self._SHIP_CURSOR_DDL)
        rows = self._conn.execute(
            "SELECT table_name, last_rowid FROM _pherix_ship_cursor"
        ).fetchall()
        return {r["table_name"]: int(r["last_rowid"]) for r in rows}

    def set_ship_cursor(self, cursor: dict) -> None:
        """Advance the durable ship cursor (idempotent UPSERT per table)."""
        self._conn.execute(self._SHIP_CURSOR_DDL)
        for table, rowid in cursor.items():
            self._conn.execute(
                "INSERT INTO _pherix_ship_cursor (table_name, last_rowid) "
                "VALUES (?, ?) ON CONFLICT(table_name) DO UPDATE SET "
                "last_rowid = excluded.last_rowid",
                (table, int(rowid)),
            )
        self._conn.commit()
