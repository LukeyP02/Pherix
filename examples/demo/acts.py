"""The three acts — scripted agent actions driven through the real engine.

Each act runs a matched pair (ungoverned vs governed) and returns a small
``ActResult`` the runner narrates and the board inlines. Everything here is
deterministic: in-memory SQLite, an in-memory "egress log" fake (no network),
a fixed action script, no clock-dependent logic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from pherix import (
    AuditJournal,
    GateBlocked,
    HTTPAdapter,
    SQLiteAdapter,
    agent_txn,
    tool,
)

# --- the seed data, fixed so every run is identical ------------------------

SEED_WIDGETS = [
    ("flux-capacitor", "in_stock"),
    ("sonic-driver", "in_stock"),
    ("warp-coil", "in_stock"),
    ("phase-inducer", "in_stock"),
    ("tachyon-lens", "in_stock"),
]

# --- a deterministic in-memory "payment egress log" ------------------------
#
# A real Pherix user calls requests / httpx / a payments SDK here. The demo
# keeps it offline so the whole thing runs anywhere with no key, no network,
# no flakiness. The list IS the observable: a charge that "fires" appends a
# row; a charge that is gated leaves the list empty.


class EgressLog:
    """Records payments that actually left the process."""

    def __init__(self) -> None:
        self.charges: list[dict] = []

    def charge(self, vendor: str, amount_cents: int, memo: str) -> dict:
        self.charges.append(
            {"vendor": vendor, "amount_cents": amount_cents, "memo": memo}
        )
        return {"charge_id": f"ch_{len(self.charges)}", "vendor": vendor}

    def reset(self) -> None:
        self.charges.clear()


EGRESS = EgressLog()


# --- the agent's @tool surface (registered once at import) -----------------


@tool(resource="sql")
def purge_discontinued(conn):
    """The agent MEANT to delete only discontinued widgets, but shipped a
    WHERE-less DELETE — the classic blast-radius mistake. It wipes the table."""
    conn.execute("DELETE FROM widgets")


@tool(resource="http", reversible=False, injects_handle=False)
def send_payment(vendor: str, amount_cents: int, memo: str):
    """An irreversible wire transfer. No compensator is registered — you
    cannot reliably claw a payment back — so under Pherix this effect cannot
    auto-commit; it must be explicitly approved or it is gated."""
    return EGRESS.charge(vendor, amount_cents, memo)


# --- helpers ---------------------------------------------------------------


def _fresh_db() -> sqlite3.Connection:
    """A fresh in-memory widgets table with the fixed seed. isolation_level=None
    (autocommit) lets the adapter own BEGIN / SAVEPOINT / COMMIT / ROLLBACK."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO widgets (name, status) VALUES (?, ?)", SEED_WIDGETS
    )
    return conn


def _row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]


@dataclass
class ActResult:
    """What the runner narrates and the board inlines for one pillar."""

    marker: str  # blast_radius / oversight / audit
    title: str
    without_label: str = ""  # e.g. "5 rows deleted"
    with_label: str = ""  # e.g. "5 rows restored"
    contained: bool = False  # did Pherix contain the mistake?
    txn_ids: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # narration


# --- Act 1 — Blast radius (reversible) -------------------------------------


def act1_blast_radius(audit: AuditJournal) -> ActResult:
    res = ActResult(marker="blast_radius", title="Blast radius")
    out = res.lines.append

    # WITHOUT Pherix — plain sqlite, no transaction wrapper.
    conn = _fresh_db()
    before = _row_count(conn)
    conn.execute("DELETE FROM widgets")  # the same WHERE-less mistake, ungoverned
    after = _row_count(conn)
    conn.close()
    out(f"WITHOUT Pherix : {before} widgets before  ->  {after} after the DELETE")
    out("                 the rows are gone; there is no undo.")
    res.without_label = f"{before - after} rows deleted, no undo"

    # WITH Pherix — the same action, wrapped; rollback restores byte-exact.
    conn = _fresh_db()
    g_before = _row_count(conn)
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
        purge_discontinued()
        mid = _row_count(conn)
        res.txn_ids.append(txn.txn_id)
        txn.rollback()  # the operator catches the mistake; unwind the savepoint
    g_after = _row_count(conn)
    state = txn.txn.state.name
    conn.close()
    out(
        f"WITH Pherix    : {g_before} before  ->  {mid} inside the txn  ->  "
        f"{g_after} after rollback()"
    )
    out(f"                 savepoint rollback restored the table; txn = {state}.")

    res.with_label = f"{g_before} rows restored, byte-exact"
    res.contained = g_after == g_before == before
    return res


# --- Act 2 — Oversight (irreversible — the wedge) --------------------------


def act2_oversight(audit: AuditJournal) -> ActResult:
    res = ActResult(marker="oversight", title="Oversight")
    out = res.lines.append

    PAYMENT = dict(vendor="acme-corp", amount_cents=480000, memo="duplicate invoice")

    # WITHOUT Pherix — the irreversible effect just fires.
    EGRESS.reset()
    EGRESS.charge(**PAYMENT)  # the agent's wrong payment, ungoverned
    fired = list(EGRESS.charges)
    out(f"WITHOUT Pherix : send_payment fires immediately -> egress log: {fired}")
    out("                 $4,800.00 is gone; the wrong/duplicate payment left.")
    res.without_label = f"{len(fired)} charge fired, ${PAYMENT['amount_cents']/100:,.2f} gone"

    # WITH Pherix — staged + GATED; the charge never fires.
    EGRESS.reset()
    gated = False
    state = "?"
    try:
        with agent_txn(
            {"sql": SQLiteAdapter(_fresh_db()), "http": HTTPAdapter()}, audit=audit
        ) as txn:
            staged = send_payment(**PAYMENT)
            res.txn_ids.append(txn.txn_id)
            _ = staged  # the sentinel placeholder the agent receives in lieu of a result
            out(
                "WITH Pherix    : send_payment is STAGED, not sent "
                "(a sentinel placeholder is returned to the agent)"
            )
            # No approve_irreversible() call -> commit blocks at the gate.
    except GateBlocked:
        gated = True
        state = txn.txn.state.name
        out(
            "                 commit GATED: the un-approved irreversible effect "
            "needs approve_irreversible()."
        )
    held = list(EGRESS.charges)
    out(
        f"                 egress log: {held}  ->  the charge NEVER fired; "
        f"txn = {state}."
    )

    res.with_label = "0 charges fired, gated for approval" if gated else "ERROR: not gated"
    res.contained = gated and held == []
    return res


# --- Act 3 — Audit ---------------------------------------------------------


def act3_audit(audit: AuditJournal, governed_txn_ids: list[str]) -> ActResult:
    """No new engine action — Act 3 reads back the journal the governed runs
    of Acts 1 & 2 already wrote. The journal IS the audit log."""
    res = ActResult(marker="audit", title="Audit")
    out = res.lines.append

    # Stable labels instead of the engine's random txn_ids, so the narrated
    # journal is byte-identical every run (the real ids live in the DB the
    # inspector opens — printed at the end of the run).
    labels = ["txn#1 (Act 1, blast radius)", "txn#2 (Act 2, oversight)"]
    total_effects = 0
    for i, tid in enumerate(governed_txn_ids):
        label = labels[i] if i < len(labels) else f"txn#{i + 1}"
        record = audit.get_transaction(tid)
        state = record["state"] if record else "?"
        effects = audit.get_effects(tid)
        total_effects += len(effects)
        out(f"{label}  state={state}")
        for e in effects:
            out(
                f"    [{e['idx']}] {e['resource']:>4}  {e['tool']:<20} "
                f"-> {e['status']}"
            )
        verdicts = audit.get_verdicts(tid)
        if verdicts:
            out(f"    {len(verdicts)} policy verdict(s) recorded")

    res.without_label = "no record — gone"
    res.with_label = f"{total_effects} effects across {len(governed_txn_ids)} txns recorded"
    res.contained = total_effects > 0
    return res
