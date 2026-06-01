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

# A transaction is *settled* once it has reached a terminal state — the
# forward/backward fold has run to completion and the txn will not change
# again. OPEN and STAGED are in-flight (the body is still running, or the
# commit is mid-bracket holding staged irreversibles), so they are excluded
# from outcome RATES: a rate over a denominator that still contains in-flight
# txns would drift as those txns settle. The reliability outcome rates are
# therefore taken over settled txns only; in-flight txns are reported
# separately (the held-back chains).
SETTLED_STATES = ("COMMITTED", "ROLLED_BACK", "PARTIAL", "STUCK")

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


def _rate(numerator: int, denominator: int) -> float:
    """A rate that is total over a denominator, guarding the empty case.

    A reliability rate is a fraction ``k / n``; on an empty journal (or an
    empty settled set) ``n = 0`` and the honest value is ``0.0``, not a
    ``ZeroDivisionError``. The empty-journal zero-rate guard the spec calls
    for lives here, in one place every rate routes through.
    """
    return (numerator / denominator) if denominator else 0.0


# --- lineage (action-provenance) primitives ---------------------------------
#
# A read/write key persisted in the journal is ``[resource, key, version]``
# (or ``[resource, key]`` for an adapter whose writes carry no version, e.g.
# the filesystem). These helpers normalise that shape for the lineage fold.

# The honest-scope statement. It travels *with* the lineage payload (returned
# as ``caveat``) so any consumer — the inspector, an exported report, a buyer's
# own tool — sees exactly what the relation does and does not claim. This is
# the action/data-lineage boundary the spec demands be explicit.
LINEAGE_CAVEAT = (
    "Action provenance only. Edges are folded from the journal's recorded "
    "read/write keys and the resources' own version counters. A 'produces' "
    "edge is version-grounded — a read observed the exact version a write "
    "produced, a fact the journal can prove. An 'informs' edge is "
    "co-transactional ordering — a read preceded a write inside the same "
    "atomic transaction — NOT proven value-flow. Pherix does NOT trace data "
    "lineage through the agent/LLM's context: it cannot see that a value a "
    "tool read actually shaped a value the agent later wrote, only that the "
    "journal records the read before the write. Full data lineage through the "
    "model is out of scope and not claimed."
)


def _lineage_key(entry: Any) -> dict:
    """Normalise a persisted key triple into ``{resource, key, version}``.

    Tolerates the two-element ``[resource, key]`` form (no version, as the
    filesystem adapter writes) and anything malformed (degrades to a best-
    effort dict rather than raising — the reader renders what it finds).
    """
    if not isinstance(entry, (list, tuple)) or not entry:
        return {"resource": None, "key": entry, "version": None}
    resource = entry[0]
    key = entry[1] if len(entry) > 1 else None
    version = entry[2] if len(entry) > 2 else None
    return {"resource": resource, "key": key, "version": version}


def _freeze(value: Any) -> Any:
    """Recursively convert lists to tuples so a parsed key is hashable.

    Keys arrive from JSON as lists (``["releases", "current"]``); the producer
    index keys on ``(resource, frozen_key, version)`` so two reads/writes of the
    same logical key collide regardless of which transaction wrote them.
    """
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


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
        # NULL-tolerance: a journal written before the ``actor`` column existed
        # has an ``effects`` table without it. The reader opens read-only and
        # cannot migrate, so it probes once at open and degrades gracefully —
        # the timeline's per-effect ``actor`` falls back to ``None`` and the
        # ``stats`` actor roll-up is simply empty for a pre-actor journal.
        self._has_actor = self._column_exists("effects", "actor")
        # The conflicts table (Prong #2) is optional in exactly the same way
        # the verdicts table is: a journal written before conflict recording
        # simply has none, and the reader degrades to "zero conflicts" rather
        # than failing to load.
        self._has_conflicts = self._table_exists("conflicts")

    # --- introspection ------------------------------------------------------

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def _column_exists(self, table: str, column: str) -> bool:
        # ``table`` is a code constant, never agent input — safe to interpolate
        # into the PRAGMA (which cannot be parameterised regardless).
        return any(
            row["name"] == column
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        )

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
                    # ``actor`` (on-whose-authority) — NULL-tolerant: a journal
                    # written before the column existed simply lacks the key,
                    # so ``.get`` degrades to ``None`` rather than KeyError-ing.
                    # The reader opens read-only and never migrates, so this
                    # graceful default is the *only* protection against an old
                    # journal here.
                    "actor": e.get("actor"),
                    "args": _loads(e["args"], {}),
                    "result": _loads(e["result"], None),
                    "read_keys": _loads(e["read_keys"], []),
                    "write_keys": _loads(e["write_keys"], []),
                    "ts": e["ts"],
                    "policy_verdicts": verdicts_by_index.get(e["idx"], []),
                }
            )
        # Prong #2: the conflicts (if any) attach to the transaction, not to
        # an effect — a conflict is a property of the txn's read-set against
        # the world at commit, spanning the whole journal, so it rides
        # alongside the effect list rather than inside it.
        return {
            "transaction": summary,
            "effects": effects,
            "conflicts": self.get_conflicts(txn_id),
        }

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

    # --- isolation conflicts (optional table, Prong #2) --------------------

    def get_conflicts(self, txn_id: str) -> list[dict]:
        """Recorded isolation conflicts for one transaction, oldest-first.

        Each row carries the parsed ``key`` tuple and the three versions
        (``version_at_read`` / ``version_now`` / ``version_expected``), so a
        console can render *what moved* without re-parsing JSON. Empty when
        the journal predates conflict recording (no table) or the txn never
        conflicted — the two are indistinguishable to a reader and both mean
        "nothing to show", so neither raises.
        """
        if not self._has_conflicts:
            return []
        rows = self._conn.execute(
            "SELECT * FROM conflicts WHERE txn_id = ? ORDER BY seq",
            (txn_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append(
                {
                    "seq": d["seq"],
                    "resource": d["resource"],
                    "key": _loads(d["key"], []),
                    "version_at_read": _loads(d["version_at_read"], None),
                    "version_now": _loads(d["version_now"], None),
                    "version_expected": _loads(d["version_expected"], None),
                    "ts": d["ts"],
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
        # Distinct actors (on-whose-authority principals) seen in the journal —
        # the actor-axis parallel to ``clients``. NULL-tolerant: empty for a
        # pre-actor journal that has no such column (see ``_has_actor``).
        actors: list[str] = []
        if self._has_actor:
            actors = [
                row[0]
                for row in self._conn.execute(
                    "SELECT DISTINCT actor FROM effects "
                    "WHERE actor IS NOT NULL ORDER BY actor"
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
        # Prong #2: the headline conflict count. Zero — not absent — when the
        # journal predates conflict recording, so the dashboard always has a
        # number to show and an old journal reads as "no conflicts seen"
        # rather than erroring.
        conflict_total = 0
        if self._has_conflicts:
            conflict_total = self._conn.execute(
                "SELECT COUNT(*) FROM conflicts"
            ).fetchone()[0]
        return {
            "txn_total": txn_total,
            "txns_by_state": by_state,
            "effect_total": effect_total,
            "clients": clients,
            "actors": actors,
            "tools": tools,
            "has_verdicts": verdict_rows > 0,
            "conflict_total": conflict_total,
        }

    # --- reliability metrics (Prong #2) ------------------------------------

    def reliability(self, *, include_dry_run: bool = False) -> dict:
        """A pure GROUP-BY fold over the journal into reliability metrics.

        Everything here is a traversal of the journal already on disk — no
        engine import, no recomputation, no writes. The shape:

        ``scope``
            What was counted. ``include_dry_run`` (the caller's choice;
            ``False`` by default — a dry-run touched nothing, so folding it
            into "how often do real txns commit?" would lie). ``settled_states``
            names the denominator the outcome rates are taken over. The denial
            rollup is the one section computed over **all** verdicts
            regardless of ``include_dry_run`` — a dry-run's denials are real
            policy decisions worth surfacing — so its scope is stated
            explicitly here (``denials_scope``) rather than left implicit.

        ``outcomes``
            Transaction-outcome rates over *settled* txns
            (commit / rollback / partial / stuck). ``settled`` is the
            denominator; ``rates`` divides each terminal count by it.
            Zero-settled → all-zero rates (no division by zero).

        ``effects``
            Effect-outcome rates over every effect in the counted txns:
            ``gate`` (GATED), ``failure`` (FAILED), ``compensated``
            (COMPENSATED), each as a fraction of the effect total. Plus
            ``gate_incidence`` — the fraction of counted txns carrying at
            least one gated effect (txn-level, not effect-level).

        ``top_failing_tools``
            Tools ranked by how often they end FAILED or GATED — never
            COMPENSATED (a compensated effect *succeeded* and was then cleanly
            undone, which is the system working, not a tool failing).
            Ranking: total desc, then *failed-before-gated* (a hard FAILED is
            a worse signal than a policy GATE), then tool name for stable
            ordering.

        ``denials``
            A rollup of every denied policy verdict (``allow = 0``) by reason,
            commonest first. Computed over ALL verdicts (dry-run included) —
            see ``scope.denials_scope``.

        ``held_back``
            Transactions currently held at the gate — in-flight (OPEN /
            STAGED) txns that carry a GATED effect. The staged/gated chains an
            operator still has to action.

        ``conflict_total``
            The Prong #2 conflict count from :meth:`get_conflicts` /
            :meth:`stats`, surfaced here so reliability has the whole picture
            in one payload. Zero on a journal predating conflict recording.
        """
        # Dry-run filter as a reusable WHERE fragment + params. Applied to the
        # txn-scoped sections; the denial rollup deliberately ignores it.
        if include_dry_run:
            txn_where = ""
            txn_filter = "1=1"
        else:
            txn_where = " WHERE t.dry_run = 0"
            txn_filter = "t.dry_run = 0"

        # --- outcomes: txn terminal-state histogram over settled txns -------
        state_counts = {s: 0 for s in TXN_STATES}
        for (state, n) in self._conn.execute(
            "SELECT t.state, COUNT(*) FROM transactions t" + txn_where
            + " GROUP BY t.state"
        ).fetchall():
            state_counts[state] = state_counts.get(state, 0) + n
        settled = sum(state_counts[s] for s in SETTLED_STATES)
        outcome_rates = {
            "commit": _rate(state_counts["COMMITTED"], settled),
            "rollback": _rate(state_counts["ROLLED_BACK"], settled),
            "partial": _rate(state_counts["PARTIAL"], settled),
            "stuck": _rate(state_counts["STUCK"], settled),
        }

        # --- effects: status histogram over the counted txns ----------------
        # Effects join to their txn so the dry-run filter applies; without the
        # join a dry-run's effects would leak into the effect rates.
        eff_counts = {s: 0 for s in EFFECT_STATUSES}
        for (status, n) in self._conn.execute(
            "SELECT e.status, COUNT(*) FROM effects e "
            "JOIN transactions t ON t.txn_id = e.txn_id "
            "WHERE " + txn_filter + " GROUP BY e.status"
        ).fetchall():
            eff_counts[status] = eff_counts.get(status, 0) + n
        eff_total = sum(eff_counts.values())
        effect_rates = {
            "gate": _rate(eff_counts["GATED"], eff_total),
            "failure": _rate(eff_counts["FAILED"], eff_total),
            "compensated": _rate(eff_counts["COMPENSATED"], eff_total),
        }

        # --- gate incidence: counted txns with >=1 gated effect -------------
        counted_txns = self._conn.execute(
            "SELECT COUNT(*) FROM transactions t" + txn_where
        ).fetchone()[0]
        gated_txns = self._conn.execute(
            "SELECT COUNT(DISTINCT e.txn_id) FROM effects e "
            "JOIN transactions t ON t.txn_id = e.txn_id "
            "WHERE " + txn_filter + " AND e.status = 'GATED'"
        ).fetchone()[0]
        gate_incidence = _rate(gated_txns, counted_txns)

        # --- top-failing tools: FAILED / GATED, never COMPENSATED -----------
        tool_rows = self._conn.execute(
            "SELECT e.tool, e.status, COUNT(*) AS n FROM effects e "
            "JOIN transactions t ON t.txn_id = e.txn_id "
            "WHERE " + txn_filter + " AND e.status IN ('FAILED', 'GATED') "
            "GROUP BY e.tool, e.status"
        ).fetchall()
        per_tool: dict[str, dict] = {}
        for r in tool_rows:
            entry = per_tool.setdefault(
                r["tool"], {"tool": r["tool"], "failed": 0, "gated": 0, "total": 0}
            )
            if r["status"] == "FAILED":
                entry["failed"] += r["n"]
            else:  # GATED
                entry["gated"] += r["n"]
            entry["total"] += r["n"]
        # Rank: total desc, then failed-before-gated (more FAILED ranks
        # higher among equal totals), then tool name for determinism.
        top_failing_tools = sorted(
            per_tool.values(),
            key=lambda e: (-e["total"], -e["failed"], e["tool"]),
        )

        # --- denial-reason rollup: ALL verdicts, dry-run included -----------
        denials: list[dict] = []
        if self._has_verdicts:
            denial_rows = self._conn.execute(
                "SELECT reason, COUNT(*) AS n FROM verdicts "
                "WHERE allow = 0 GROUP BY reason ORDER BY n DESC, reason"
            ).fetchall()
            denials = [
                {"reason": r["reason"], "count": r["n"]} for r in denial_rows
            ]

        # --- held-back chains: in-flight txns holding a gated effect --------
        held_rows = self._conn.execute(
            "SELECT DISTINCT t.txn_id, t.state FROM transactions t "
            "JOIN effects e ON e.txn_id = t.txn_id "
            "WHERE " + txn_filter
            + " AND t.state IN ('OPEN', 'STAGED') AND e.status = 'GATED' "
            "ORDER BY t.txn_id"
        ).fetchall()
        held_back = [{"txn_id": r["txn_id"], "state": r["state"]} for r in held_rows]

        # --- conflicts: the Prong #2 first-class record ---------------------
        conflict_total = 0
        if self._has_conflicts:
            conflict_total = self._conn.execute(
                "SELECT COUNT(*) FROM conflicts"
            ).fetchone()[0]

        return {
            "scope": {
                "include_dry_run": include_dry_run,
                "settled_states": list(SETTLED_STATES),
                # The denial rollup spans every verdict regardless of the
                # dry-run choice above — stated, not implied.
                "denials_scope": "all_verdicts",
            },
            "outcomes": {
                "settled": settled,
                "counts": {s: state_counts[s] for s in SETTLED_STATES},
                "rates": outcome_rates,
            },
            "effects": {
                "total": eff_total,
                "counts": eff_counts,
                "rates": effect_rates,
                "gate_incidence": gate_incidence,
            },
            "top_failing_tools": top_failing_tools,
            "denials": denials,
            "held_back": held_back,
            "conflict_total": conflict_total,
        }

    # --- lineage (causal read→write provenance) ----------------------------

    def lineage(self, txn_id: str | None = None) -> dict:
        """Fold the journal's read/write keys into causal read→write chains.

        This answers the provenance question — *"this write was informed by
        these reads, with these verdicts"* — as a pure traversal of the same
        append-only journal everything else folds over. Nothing is recomputed
        from the live world; the relation is read entirely off the persisted
        ``read_keys`` / ``write_keys`` and the resources' version counters.

        Two relations, both derived (never stored):

        - **produces** (version-grounded, the strong claim): a read that
          observed ``(resource, key, version)`` is *produced by* the write
          whose recorded post-version is exactly that ``version``. The version
          counter ties reader to writer, so this is provable from the journal
          — a genuine read-after-write data edge, even across transactions.
        - **informs** (co-transactional ordering, the weaker claim): inside one
          transaction, a read at index *i* informs every write at index *j ≥ i*
          — the read happened before the write in the same atomic unit. Honest
          ordering, **not** proven value-flow (see :data:`LINEAGE_CAVEAT`).

        ``txn_id`` scopes the *focus* (which writers get a chain, which effects
        are nodes); upstream producers are still resolved against the **whole**
        journal, so a chain shows when one transaction's write fed another's
        read. ``txn_id=None`` folds the entire journal.

        Returns a render-ready dict — ``scope`` (counts), ``nodes`` (effects
        that read/write, each tagged ``in_focus``), ``edges`` (the causal
        graph, every endpoint present in ``nodes``), ``chains`` (per-writer
        provenance, the headline view) and ``caveat`` (the scope statement,
        carried with the data). Everything is plain JSON-serialisable.
        """
        rows = self._conn.execute(
            "SELECT txn_id, idx, tool, resource, status, read_keys, write_keys "
            "FROM effects ORDER BY txn_id, idx"
        ).fetchall()

        # Normalise every effect once: parsed reads/writes + a stable node id.
        effects: list[dict] = []
        for r in rows:
            d = dict(r)
            effects.append(
                {
                    "node": f"{d['txn_id']}#{d['idx']}",
                    "txn_id": d["txn_id"],
                    "idx": d["idx"],
                    "tool": d["tool"],
                    "resource": d["resource"],
                    "status": d["status"],
                    "reads": [_lineage_key(k) for k in _loads(d["read_keys"], [])],
                    "writes": [_lineage_key(k) for k in _loads(d["write_keys"], [])],
                }
            )
        node_meta = {e["node"]: e for e in effects}

        # Producer index: which write produced each (resource, key, version).
        # Versionless writes (filesystem) can't anchor a version-grounded edge,
        # so they're skipped here — they still appear as writers in chains.
        producers: dict[tuple, str] = {}
        for e in effects:
            for w in e["writes"]:
                if w["version"] is None:
                    continue
                sig = (w["resource"], _freeze(w["key"]), w["version"])
                producers.setdefault(sig, e["node"])

        by_txn: dict[str, list[dict]] = {}
        for e in effects:  # already idx-ordered within each txn by the query
            by_txn.setdefault(e["txn_id"], []).append(e)

        focus = {
            e["node"]
            for e in effects
            if txn_id is None or e["txn_id"] == txn_id
        }
        focus_txns = {node_meta[n]["txn_id"] for n in focus}

        # Per-effect policy verdicts for the focus transactions (the "with
        # these verdicts" half of the provenance claim).
        verdicts_by_node: dict[str, list[dict]] = {}
        if self._has_verdicts:
            for tid in focus_txns:
                for idx, vs in self._verdicts_by_index(tid).items():
                    verdicts_by_node[f"{tid}#{idx}"] = vs

        edges: list[dict] = []
        referenced: set[str] = set()

        def _producer_of(rd: dict) -> str | None:
            return producers.get((rd["resource"], _freeze(rd["key"]), rd["version"]))

        # Pass A — produces edges (read-after-write) for every focus read, so a
        # read whose value came from an earlier write shows that provenance even
        # if the reading effect writes nothing itself.
        for e in effects:
            if e["node"] not in focus:
                continue
            for rd in e["reads"]:
                producer = _producer_of(rd)
                if producer is not None and producer != e["node"]:
                    edges.append(
                        {
                            "from": producer,
                            "to": e["node"],
                            "kind": "produces",
                            "resource": rd["resource"],
                            "key": rd["key"],
                            "version": rd["version"],
                            "detail": (
                                f"{node_meta[producer]['tool']} wrote "
                                f"{rd['resource']}:{rd['key']} v{rd['version']}, "
                                f"read by {e['tool']}"
                            ),
                        }
                    )
                    referenced.add(producer)
                    referenced.add(e["node"])

        # Pass B — chains + informs edges, one chain per focus writer.
        chains: list[dict] = []
        for e in effects:
            if e["node"] not in focus or not e["writes"]:
                continue
            informed_by: list[dict] = []
            for other in by_txn[e["txn_id"]]:
                if other["idx"] > e["idx"]:
                    break  # idx-ordered: nothing past the writer can precede it
                for rd in other["reads"]:
                    producer = _producer_of(rd)
                    same_effect = other["node"] == e["node"]
                    informed_by.append(
                        {
                            "node": other["node"],
                            "txn_id": other["txn_id"],
                            "idx": other["idx"],
                            "tool": other["tool"],
                            "resource": rd["resource"],
                            "key": rd["key"],
                            "version": rd["version"],
                            "same_effect": same_effect,
                            "produced_by": producer,
                            # No producer in this journal → the value's origin
                            # predates it or lives elsewhere. Honest, not a gap.
                            "produced_by_external": producer is None,
                        }
                    )
                    if not same_effect:
                        edges.append(
                            {
                                "from": other["node"],
                                "to": e["node"],
                                "kind": "informs",
                                "resource": rd["resource"],
                                "key": rd["key"],
                                "version": rd["version"],
                                "detail": (
                                    f"{other['tool']} read {rd['resource']}:"
                                    f"{rd['key']} before {e['tool']} wrote"
                                ),
                            }
                        )
                        referenced.add(other["node"])
            referenced.add(e["node"])
            v = effect_verdict(e["status"])
            chains.append(
                {
                    "node": e["node"],
                    "txn_id": e["txn_id"],
                    "idx": e["idx"],
                    "tool": e["tool"],
                    "resource": e["resource"],
                    "status": e["status"],
                    "verdict": v["verdict"],
                    "tone": v["tone"],
                    "writes": e["writes"],
                    "informed_by": informed_by,
                    "policy_verdicts": verdicts_by_node.get(e["node"], []),
                }
            )

        # Dedupe edges (a reader→writer pair repeats per shared key; keep one
        # per (from, to, kind, resource, key)).
        seen: set[tuple] = set()
        unique_edges: list[dict] = []
        for ed in edges:
            sig = (ed["from"], ed["to"], ed["kind"], ed["resource"],
                   json.dumps(ed["key"], sort_keys=True))
            if sig in seen:
                continue
            seen.add(sig)
            unique_edges.append(ed)

        # Nodes = focus effects + any external producer feeding them, so every
        # edge endpoint resolves to a node the UI can draw.
        node_ids = focus | referenced
        nodes = []
        for nid in sorted(node_ids):
            e = node_meta[nid]
            v = effect_verdict(e["status"])
            nodes.append(
                {
                    "node": nid,
                    "txn_id": e["txn_id"],
                    "idx": e["idx"],
                    "tool": e["tool"],
                    "resource": e["resource"],
                    "status": e["status"],
                    "verdict": v["verdict"],
                    "tone": v["tone"],
                    "in_focus": nid in focus,
                    "reads": e["reads"],
                    "writes": e["writes"],
                }
            )

        return {
            "scope": {
                "txn_id": txn_id,
                "node_count": len(nodes),
                "edge_count": len(unique_edges),
                "chain_count": len(chains),
            },
            "nodes": nodes,
            "edges": unique_edges,
            "chains": chains,
            "caveat": LINEAGE_CAVEAT,
        }
