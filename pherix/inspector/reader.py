"""Read-only query layer over the Pherix audit journal.

The :class:`pherix.core.audit.AuditJournal` exposes only ``get_transaction``
and ``get_effects`` by id — enough for the engine, not enough for a console.
This module adds the *read* side a governance view needs: list transactions
with filters, fold a transaction into a render-ready timeline, derive the
effective per-effect verdict from persisted status, and roll up summary
stats — all without writing, and without importing the engine.

It opens the database in read-only mode (``?mode=ro``) so the inspector can
never mutate the journal it is auditing. Everything returned is plain
JSON-serialisable dicts/lists, ready to hand to the HTTP layer untouched.

Why no engine import: the reader must render a journal written by *any*
Pherix version that preserves the table shapes, including one produced on a
different machine and copied over for a post-mortem. Coupling it to the
live ``TxnState`` / ``EffectStatus`` enums would make a schema the reader
can already parse fail to load because an enum gained a member. The status
strings are the contract; we read them as text.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# --- the status vocabulary (the persisted contract) -------------------------
#
# These mirror EffectStatus / TxnState `.name` values as written by
# audit.py. Kept as plain strings (not enum imports) so the reader stays
# decoupled from the engine — see the module docstring.

EFFECT_STATUSES = ("STAGED", "APPLIED", "COMPENSATED", "GATED", "FAILED")
TXN_STATES = ("OPEN", "STAGED", "COMMITTED", "ROLLED_BACK", "PARTIAL", "STUCK")

# Effective per-effect verdict derived from the persisted status. This is the
# honest, schema-backed reading of "what the policy/engine decided about this
# effect" — distinct from the optional per-rule verdict rows (see
# get_verdicts), which carry which rule/cap fired and at which phase.
#
# tone: "ok" reads neutral/green, "pending" amber, "blocked"/"undone"/"error"
# the alarm colours. ``undone`` marks the backward fold so the UI can strike
# the row through.
_EFFECT_VERDICT = {
    "APPLIED": {"verdict": "applied", "tone": "ok", "undone": False,
                "blurb": "executed and committed"},
    "STAGED": {"verdict": "staged", "tone": "pending", "undone": False,
               "blurb": "irreversible, held until commit"},
    "GATED": {"verdict": "gated", "tone": "blocked", "undone": False,
              "blurb": "blocked at the gate — needs approval"},
    "COMPENSATED": {"verdict": "compensated", "tone": "undone", "undone": True,
                    "blurb": "undone by its compensator on rollback"},
    "FAILED": {"verdict": "failed", "tone": "error", "undone": False,
               "blurb": "denied or errored — never took effect"},
}

_TXN_SUMMARY = {
    "OPEN": {"tone": "pending", "blurb": "in flight"},
    "STAGED": {"tone": "pending", "blurb": "committing — irreversibles staged"},
    "COMMITTED": {"tone": "ok", "blurb": "committed cleanly"},
    "ROLLED_BACK": {"tone": "undone", "blurb": "rolled back — nothing took effect"},
    "PARTIAL": {"tone": "error", "blurb": "partial — unwinding after a mid-fire failure"},
    "STUCK": {"tone": "error", "blurb": "STUCK — a compensator was missing or failed"},
}


def effect_verdict(status: str) -> dict:
    """Effective verdict for one effect, derived from its persisted status.

    Unknown statuses (a journal from a newer engine) degrade to a neutral
    "unknown" rather than raising — the reader's job is to render what it
    finds, not to validate the writer.
    """
    return _EFFECT_VERDICT.get(
        status,
        {"verdict": status.lower(), "tone": "unknown", "undone": False,
         "blurb": status},
    )


def txn_summary(state: str) -> dict:
    return _TXN_SUMMARY.get(
        state, {"tone": "unknown", "blurb": state}
    )


def _loads(blob: Any, default: Any) -> Any:
    """Parse a JSON column, tolerating NULL and already-decoded values."""
    if blob is None:
        return default
    if not isinstance(blob, (str, bytes, bytearray)):
        return blob
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        # A column that isn't valid JSON is shown verbatim rather than dropped.
        return blob


class JournalReader:
    """Read-only window onto a Pherix audit journal.

    Opens the SQLite file in read-only mode so the inspector cannot mutate
    the journal under audit. The ``verdicts`` table is optional — a journal
    written before per-rule verdict persistence simply has none, and the
    reader degrades to the status-derived effective verdict.
    """

    def __init__(self, path: str):
        self.path = path
        # Read-only URI connection: the inspector is a console, never a writer.
        # ``check_same_thread=False`` so the ThreadingHTTPServer can share one
        # reader across request threads (reads only — SQLite serialises them).
        if path == ":memory:":
            # An in-memory journal can't be reopened read-only by URI; used by
            # tests that hand us a live handle's path is impossible, so this is
            # a writable in-memory connection (tests own it).
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self._conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
        self._conn.row_factory = sqlite3.Row
        self._has_verdicts = self._table_exists("verdicts")

    # --- introspection ------------------------------------------------------

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JournalReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- listing / filtering ------------------------------------------------

    def list_transactions(
        self,
        *,
        state: str | None = None,
        client_id: str | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_dry_run: bool = True,
        limit: int = 200,
    ) -> list[dict]:
        """Transactions newest-first, each rolled up to a render-ready summary.

        Filters compose (AND). ``tool`` matches transactions that contain at
        least one effect with that tool. ``since`` / ``until`` bound
        ``created_at`` (ISO-8601 strings; lexical compare is correct for
        ISO-8601). ``include_dry_run=False`` is the compliance view's
        ``WHERE dry_run = 0``.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("t.state = ?")
            params.append(state)
        if client_id is not None:
            clauses.append("t.client_id = ?")
            params.append(client_id)
        if not include_dry_run:
            clauses.append("t.dry_run = 0")
        if since is not None:
            clauses.append("t.created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("t.created_at <= ?")
            params.append(until)
        if tool is not None:
            clauses.append(
                "t.txn_id IN (SELECT txn_id FROM effects WHERE tool = ?)"
            )
            params.append(tool)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT t.* FROM transactions t"
            + where
            + " ORDER BY t.created_at DESC, t.txn_id DESC LIMIT ?"
        )
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [self._summarise_txn(dict(r)) for r in rows]

    def _summarise_txn(self, t: dict) -> dict:
        """Roll a transaction row up with its effect-status histogram."""
        counts = {s: 0 for s in EFFECT_STATUSES}
        total = 0
        for (status, n) in self._conn.execute(
            "SELECT status, COUNT(*) FROM effects WHERE txn_id = ? GROUP BY status",
            (t["txn_id"],),
        ).fetchall():
            counts[status] = counts.get(status, 0) + n
            total += n
        summ = txn_summary(t["state"])
        return {
            "txn_id": t["txn_id"],
            "state": t["state"],
            "tone": summ["tone"],
            "blurb": summ["blurb"],
            "created_at": t["created_at"],
            "updated_at": t["updated_at"],
            "dry_run": bool(t["dry_run"]),
            "client_id": t["client_id"],
            "replayed_from": t["replayed_from"],
            "effect_count": total,
            "status_counts": counts,
            # at-a-glance flags the timeline list colour-codes
            "has_gate": counts.get("GATED", 0) > 0,
            "has_compensation": counts.get("COMPENSATED", 0) > 0,
            "has_failure": counts.get("FAILED", 0) > 0,
            "is_stuck": t["state"] == "STUCK",
            "is_rolled_back": t["state"] == "ROLLED_BACK",
        }

    # --- one transaction's timeline ----------------------------------------

    def get_timeline(self, txn_id: str) -> dict | None:
        """The full render-ready timeline for one transaction, or None.

        Returns the transaction summary plus an ordered list of effects, each
        with parsed args / read-keys / write-keys, the derived effective
        verdict, and any per-rule policy verdicts attached to that effect.
        """
        trow = self._conn.execute(
            "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        if trow is None:
            return None
        summary = self._summarise_txn(dict(trow))

        verdicts_by_index = self._verdicts_by_index(txn_id)
        erows = self._conn.execute(
            "SELECT * FROM effects WHERE txn_id = ? ORDER BY idx", (txn_id,)
        ).fetchall()
        effects = []
        for r in erows:
            e = dict(r)
            v = effect_verdict(e["status"])
            effects.append(
                {
                    "idx": e["idx"],
                    "effect_id": e["effect_id"],
                    "tool": e["tool"],
                    "resource": e["resource"],
                    "reversible": bool(e["reversible"]),
                    "status": e["status"],
                    "verdict": v["verdict"],
                    "tone": v["tone"],
                    "undone": v["undone"],
                    "blurb": v["blurb"],
                    "args": _loads(e["args"], {}),
                    "result": _loads(e["result"], None),
                    "read_keys": _loads(e["read_keys"], []),
                    "write_keys": _loads(e["write_keys"], []),
                    "ts": e["ts"],
                    "policy_verdicts": verdicts_by_index.get(e["idx"], []),
                }
            )
        return {"transaction": summary, "effects": effects}

    # --- per-rule policy verdicts (optional table) -------------------------

    def _verdicts_by_index(self, txn_id: str) -> dict[int, list[dict]]:
        """Per-effect policy verdicts, grouped by effect index.

        Empty when the journal predates verdict persistence (no table) — the
        timeline then carries only the status-derived effective verdict.
        """
        if not self._has_verdicts:
            return {}
        out: dict[int, list[dict]] = {}
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE txn_id = ? "
            "ORDER BY effect_index, seq",  # seq encodes stage-before-commit
            (txn_id,),
        ).fetchall()
        for r in rows:
            d = dict(r)
            out.setdefault(d["effect_index"], []).append(
                {
                    "phase": d["phase"],          # 'stage' | 'commit'
                    "allow": bool(d["allow"]),
                    "rule": d["rule_name"],
                    "kind": d["kind"],            # 'rule' | 'cap' | 'allowlist'
                    "reason": d["reason"],
                }
            )
        return out

    # --- summary stats ------------------------------------------------------

    def stats(self) -> dict:
        """Headline counts for the dashboard — txns by state, effect totals."""
        by_state = {s: 0 for s in TXN_STATES}
        for (state, n) in self._conn.execute(
            "SELECT state, COUNT(*) FROM transactions GROUP BY state"
        ).fetchall():
            by_state[state] = by_state.get(state, 0) + n
        txn_total = sum(by_state.values())
        effect_total = self._conn.execute(
            "SELECT COUNT(*) FROM effects"
        ).fetchone()[0]
        clients = [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT client_id FROM transactions "
                "WHERE client_id IS NOT NULL ORDER BY client_id"
            ).fetchall()
        ]
        tools = [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT tool FROM effects ORDER BY tool"
            ).fetchall()
        ]
        # "has_verdicts" drives the console's per-rule indicator, so it means
        # "there is at least one verdict to show" — table present AND populated
        # — not merely that a (possibly empty) table exists.
        verdict_rows = 0
        if self._has_verdicts:
            verdict_rows = self._conn.execute(
                "SELECT COUNT(*) FROM verdicts"
            ).fetchone()[0]
        return {
            "txn_total": txn_total,
            "txns_by_state": by_state,
            "effect_total": effect_total,
            "clients": clients,
            "tools": tools,
            "has_verdicts": verdict_rows > 0,
        }
