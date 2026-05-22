"""Shared scaffolding for the fuzzing / adversarial suites (``test_fuzz_*.py``).

Not a test module — the leading underscore keeps pytest from collecting it.

Two jobs:

1. Build a *valid* durable journal that looks exactly like a crash left it — a
   mid-backward-fold transaction with a real APPLIED irreversible standing in
   the world — so the corruption suite has a known-good baseline to mutate. The
   property the corruption suite pins is that ``recover`` on a *corrupted* copy
   of that baseline either lands a correct exactly-once terminal state or raises
   a clear typed error; it must NEVER return a success report while leaving an
   effect half-applied / double-applied / silently dropped.

2. Raw-SQLite corruption primitives (truncate / byte-flip / row-delete /
   column-null / JSON-mangle) that operate directly on the journal's two tables,
   plus a ``CountingAdapter`` whose ``applied`` list makes "fired exactly once"
   observable.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any, Callable

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState


class CountingAdapter:
    """Irreversible adapter recording every compensator fire.

    ``applied`` is the observable for the exactly-once property: a correct
    recovery fires one compensator per standing APPLIED irreversible and zero on
    a second pass. ``raise_on`` lets a test force a compensator failure (→ STUCK).
    """

    name = "ext"

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.applied: list[tuple[str, dict]] = []
        self._raise_on = raise_on or set()

    def supports_rollback(self) -> bool:
        return False

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        self.applied.append((effect.tool, dict(effect.args)))
        if effect.tool in self._raise_on:
            raise RuntimeError(f"compensator {effect.tool!r} forced to fail")
        return tool_fn(**effect.args)


def build_midflight_journal(
    db_path: str,
    *,
    n_charges: int = 3,
    txn_state: TxnState = TxnState.PARTIAL,
) -> str:
    """Persist a valid mid-backward-fold journal: ``n_charges`` APPLIED
    irreversible 'charge' effects under a non-terminal transaction.

    This is the known-good baseline the corruption suite mutates. Each charge
    has a registered compensator ('refund'); a clean ``recover`` against this
    file fires exactly ``n_charges`` refunds and lands ROLLED_BACK. Returns the
    txn_id so a test can target its rows for corruption.
    """
    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = txn_state
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, txn_state.name)
    for idx in range(n_charges):
        eff = Effect(
            txn_id=txn.txn_id,
            index=idx,
            tool="charge",
            args={"amount": 100 + idx},
            resource="ext",
            reversible=False,
            status=EffectStatus.APPLIED,
        )
        audit.record_effect(eff)
        audit.update_effect(eff)
    audit.close()
    return txn.txn_id


# --- file-level corruption primitives ---------------------------------------


def truncate_at(db_path: str, offset: int) -> None:
    """Truncate the SQLite file to ``offset`` bytes (clamped to file size)."""
    p = Path(db_path)
    size = p.stat().st_size
    with open(db_path, "r+b") as f:
        f.truncate(min(offset, size))


def overwrite_range(db_path: str, offset: int, blob: bytes) -> None:
    """Overwrite ``len(blob)`` bytes starting at ``offset`` (clamped)."""
    p = Path(db_path)
    size = p.stat().st_size
    if offset >= size:
        return
    with open(db_path, "r+b") as f:
        f.seek(offset)
        f.write(blob[: size - offset])


def flip_bytes(db_path: str, rng: random.Random, n: int) -> None:
    """Flip ``n`` random bytes in the file (XOR with a random non-zero mask)."""
    p = Path(db_path)
    data = bytearray(p.read_bytes())
    if not data:
        return
    for _ in range(n):
        i = rng.randrange(len(data))
        data[i] ^= rng.randint(1, 255)
    p.write_bytes(bytes(data))


# --- semantic (row/column-level) corruption primitives ----------------------
#
# These keep the SQLite container valid but make the *journal* internally
# inconsistent — the harder, more interesting corruption class. They operate
# on the real schema: tables `transactions` / `effects`, columns per audit.py.


def _raw(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None  # autocommit — every statement lands immediately
    return conn


def delete_effect_rows(db_path: str, txn_id: str, *, keep: int = 0) -> None:
    """Delete all but the lowest ``keep`` effect rows for ``txn_id``.

    Models a journal flushed only partway: the txn row + effect headers exist
    but later effect rows never made it to disk.
    """
    conn = _raw(db_path)
    try:
        conn.execute(
            "DELETE FROM effects WHERE txn_id = ? AND idx >= ?", (txn_id, keep)
        )
    finally:
        conn.close()


def null_column(db_path: str, txn_id: str, column: str, idx: int = 0) -> None:
    """NULL out one column of one effect row (semantic corruption).

    ``column`` is from a fixed allowlist (never agent input) so interpolating
    the identifier is safe — sqlite cannot parameterise identifiers anyway.
    """
    assert column in {"status", "snapshot", "args", "tool", "resource", "result"}
    conn = _raw(db_path)
    try:
        conn.execute(
            f"UPDATE effects SET {column} = NULL WHERE txn_id = ? AND idx = ?",
            (txn_id, idx),
        )
    finally:
        conn.close()


def set_effect_status(db_path: str, txn_id: str, idx: int, status: str) -> None:
    """Force one effect row's status string (incl. enum values that don't
    exist, e.g. 'BOGUS') — semantic corruption of the idempotency fence."""
    conn = _raw(db_path)
    try:
        conn.execute(
            "UPDATE effects SET status = ? WHERE txn_id = ? AND idx = ?",
            (status, txn_id, idx),
        )
    finally:
        conn.close()


def set_txn_state(db_path: str, txn_id: str, state: str) -> None:
    """Force the transaction row's state string (incl. nonexistent states)."""
    conn = _raw(db_path)
    try:
        conn.execute(
            "UPDATE transactions SET state = ? WHERE txn_id = ?", (state, txn_id)
        )
    finally:
        conn.close()


def mangle_json_column(db_path: str, txn_id: str, column: str, idx: int = 0) -> None:
    """Replace a JSON column's contents with invalid JSON.

    ``args`` is the highest-value target: ``_effect_from_row`` does
    ``json.loads(row["args"])`` unconditionally, so malformed JSON there is the
    parse-boundary corruption the recovery fold must not swallow silently.
    """
    assert column in {"args", "snapshot", "result", "read_keys", "write_keys"}
    conn = _raw(db_path)
    try:
        conn.execute(
            f"UPDATE effects SET {column} = ? WHERE txn_id = ? AND idx = ?",
            ("{not valid json,,,", txn_id, idx),
        )
    finally:
        conn.close()


def set_effect_index(db_path: str, txn_id: str, old_idx: int, new_idx: int) -> None:
    """Rewrite an effect's index (e.g. to a negative / out-of-range value)."""
    conn = _raw(db_path)
    try:
        conn.execute(
            "UPDATE effects SET idx = ? WHERE txn_id = ? AND idx = ?",
            (new_idx, txn_id, old_idx),
        )
    finally:
        conn.close()


def read_durable_statuses(db_path: str, txn_id: str) -> list[str]:
    """The per-effect status strings, idx order — the post-recovery fence."""
    conn = _raw(db_path)
    try:
        rows = conn.execute(
            "SELECT status FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def read_txn_state(db_path: str, txn_id: str) -> str | None:
    conn = _raw(db_path)
    try:
        row = conn.execute(
            "SELECT state FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
