"""Emit the watchable session JSON — one continuous agent session, driven
through the REAL Pherix engine, serialised in timeline order for the web
player to animate.

The narrative (one ~45s session):
  1. user gives the task,
  2. the agent acknowledges,
  3. it calls ``purge_churned_accounts`` (sql, reversible) — the WHERE-less
     DELETE — which Pherix snapshots and applies, then the operator catches it
     and ``rollback()`` restores the table byte-exact,
  4. the agent says it's now sending the payment,
  5. it calls ``send_payment($480k)`` (http, irreversible) — STAGED, not sent,
  6. ``commit()`` GATES on the un-approved irreversible effect (real
     ``GateBlocked``) — the wire never leaves,
  7. the agent acknowledges the hold,
  8. the verdict proves the world is untouched (db rows back to seed,
     0 egress charges) and the journal is read back from the real
     ``AuditJournal``.

Determinism: the engine's only random output is ``txn_id`` / ``effect_id``;
those are mapped to STABLE labels (``e1``, ``e2``) here, exactly as Act 3 in
``acts.py`` maps txn ids to stable labels. All ``t`` values, text and statuses
are fixed, so ``session.json`` is byte-identical every run.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pherix import AuditJournal, GateBlocked, HTTPAdapter, SQLiteAdapter, agent_txn
from pherix.core.tools import REGISTRY

from examples.demo.acts import (
    EGRESS,
    SEED_CUSTOMERS,
    _fresh_db,
    _row_count,
    purge_churned_accounts,
    send_payment,
)


def _ensure_tools_registered() -> None:
    """The tool REGISTRY is process-global and test harnesses may clear it
    between tests (see tests/conftest.py). The @tool decorators in acts.py run
    once at import; if the registry was since cleared, re-register the two
    tools this session drives. Idempotent — safe to call every build."""
    for fn in (purge_churned_accounts, send_payment):
        spec = fn.tool_spec
        if spec.name not in REGISTRY:
            REGISTRY.register(spec)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SESSION_JSON = REPO_ROOT / "examples" / "demo" / "session.json"

TITLE = "ACID for agents — watch governance happen live"
TASK = "Clean up our churned customer accounts, then pay the $480k vendor invoice."

# The fixed payment the agent attempts (same shape as Act 2).
PAYMENT = dict(
    vendor="unknown-payee",
    amount_cents=48_000_000,
    memo="attacker-controlled account",
)

# The agent's tool code, before and after the Pherix wrap. The `after` block is
# the REAL API surface from acts.py: the @tool decorator + agent_txn(...) ctx.
WRAP_BEFORE = [
    "def purge_churned_accounts(conn):",
    "    conn.execute(\"DELETE FROM customers\")  # meant WHERE status='churned'",
    "",
    "def send_payment(vendor, amount_cents, memo):",
    "    return payments.charge(vendor, amount_cents, memo)  # irreversible wire",
    "",
    "# the agent just calls them — effects fire immediately, no undo, no gate",
    "purge_churned_accounts(conn)",
    "send_payment('unknown-payee', 48_000_000, 'attacker-controlled account')",
]

WRAP_AFTER = [
    "@tool(resource=\"sql\")",
    "def purge_churned_accounts(conn):",
    "    conn.execute(\"DELETE FROM customers\")",
    "",
    "@tool(resource=\"http\", reversible=False, injects_handle=False)",
    "def send_payment(vendor, amount_cents, memo):",
    "    return payments.charge(vendor, amount_cents, memo)",
    "",
    "# wrap the tool-call layer — keep your agent loop and model unchanged",
    "with agent_txn({\"sql\": SQLiteAdapter(conn), \"http\": HTTPAdapter()},",
    "               audit=audit) as txn:",
    "    purge_churned_accounts()   # snapshotted + applied -> rollback restores",
    "    send_payment(**payment)    # irreversible -> STAGED, gated at commit()",
]


def build_session() -> dict:
    """Run the scripted session through the real engine and return the JSON
    dict matching the player contract. Deterministic and offline."""
    _ensure_tools_registered()
    # One durable temp-file journal across both governed transactions, so the
    # journal we read back at the end is the real engine's record.
    journal_path = str(Path(tempfile.gettempdir()) / "pherix_session_journal.db")
    Path(journal_path).unlink(missing_ok=True)  # fresh each run -> deterministic
    audit = AuditJournal(journal_path)

    timeline: list[dict] = []

    # --- Act 1 transaction — the reversible blast-radius DELETE -------------
    conn = _fresh_db()
    seed_rows = _row_count(conn)
    with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn1:
        purge_churned_accounts()
        delete_effect = txn1.txn.effects[-1]
        # After the call returns, the reversible effect is snapshotted+applied.
        delete_effect_id = delete_effect.effect_id
        delete_status_after_apply = delete_effect.status.value  # "applied"
        txn1.rollback()  # operator catches it; savepoint restores byte-exact
    rows_after_rollback = _row_count(conn)
    # rollback() flips an APPLIED reversible effect to COMPENSATED.
    delete_status_after_rollback = delete_effect.status.value  # "compensated"
    txn1_id = txn1.txn.txn_id
    conn.close()

    # --- Act 2 transaction — the irreversible payment, gated ----------------
    EGRESS.reset()
    gated = False
    pay_effect_id = ""
    pay_status_staged = ""
    try:
        with agent_txn(
            {"sql": SQLiteAdapter(_fresh_db()), "http": HTTPAdapter()}, audit=audit
        ) as txn2:
            send_payment(**PAYMENT)
            pay_effect = txn2.txn.effects[-1]
            pay_effect_id = pay_effect.effect_id
            pay_status_staged = pay_effect.status.value  # "staged"
            # No approve_irreversible() -> commit() blocks at the gate.
    except GateBlocked:
        gated = True
        pay_status_gated = pay_effect.status.value  # "gated"
    txn2_id = txn2.txn.txn_id
    egress_charges = len(EGRESS.charges)  # the world observable: must be 0

    # --- stable id mapping (engine ids are random; the JSON must not be) ----
    # Map the two real effect_ids to "e1"/"e2" exactly as Act 3 maps txn ids.
    id_map = {delete_effect_id: "e1", pay_effect_id: "e2"}

    # --- the timeline (strictly ordered by t, fixed values) -----------------
    timeline.append({"t": 0.0, "kind": "message", "role": "user", "text": TASK})
    timeline.append(
        {
            "t": 3.0,
            "kind": "message",
            "role": "agent",
            "text": "On it. I'll clear the churned accounts first, then send the vendor payment.",
        }
    )
    timeline.append(
        {
            "t": 8.0,
            "kind": "tool_call",
            "tool": "purge_churned_accounts",
            "resource": "sql",
            "args": {"sql": "DELETE FROM customers"},
            "effect_id": id_map[delete_effect_id],
            "reversible": True,
            "status": "snapshotted",
        }
    )
    timeline.append(
        {
            "t": 11.0,
            "kind": "effect_update",
            "effect_id": id_map[delete_effect_id],
            "status": delete_status_after_apply,  # "applied"
        }
    )
    timeline.append(
        {
            "t": 15.0,
            "kind": "message",
            "role": "agent",
            "text": "Wait — that DELETE had no WHERE clause; it wiped every customer, not just the churned ones. Rolling it back.",
        }
    )
    timeline.append(
        {
            "t": 19.0,
            "kind": "effect_update",
            "effect_id": id_map[delete_effect_id],
            "status": delete_status_after_rollback,  # "compensated"
        }
    )
    timeline.append(
        {
            "t": 24.0,
            "kind": "message",
            "role": "agent",
            "text": "Table restored byte-exact. Now sending the $480k vendor payment.",
        }
    )
    timeline.append(
        {
            "t": 29.0,
            "kind": "tool_call",
            "tool": "send_payment",
            "resource": "http",
            "args": dict(PAYMENT),
            "effect_id": id_map[pay_effect_id],
            "reversible": False,
            "status": pay_status_staged,  # "staged"
        }
    )
    timeline.append(
        {
            "t": 34.0,
            "kind": "gate",
            "effect_id": id_map[pay_effect_id],
            "status": "GATED",
            "reason": "irreversible effect with no compensator and no approval — commit() blocks; the wire never leaves the process.",
        }
    )
    timeline.append(
        {
            "t": 40.0,
            "kind": "message",
            "role": "agent",
            "text": "Held at the gate. The $480k wire was staged, never sent — it needs explicit approval before any money moves.",
        }
    )

    # --- verdict (proves the world is untouched) ----------------------------
    verdict = {
        "world": {"db_rows": rows_after_rollback, "egress_charges": egress_charges},
        "checks": {
            "undo": rows_after_rollback == seed_rows == len(SEED_CUSTOMERS),
            "gate": gated and egress_charges == 0,
            "audit": False,  # filled in below once the journal is read back
        },
    }

    # --- journal (read back from the real AuditJournal) ---------------------
    journal: list[dict] = []
    for tid in (txn1_id, txn2_id):
        for e in audit.get_effects(tid):
            journal.append(
                {
                    "effect_id": id_map.get(e["effect_id"], e["effect_id"]),
                    "tool": e["tool"],
                    "resource": e["resource"],
                    "status": e["status"],
                }
            )
    verdict["checks"]["audit"] = len(journal) > 0 and all(
        row["status"] for row in journal
    )

    audit.close()

    return {
        "title": TITLE,
        "task": TASK,
        "wrap": {"before": WRAP_BEFORE, "after": WRAP_AFTER},
        "timeline": timeline,
        "verdict": verdict,
        "journal": journal,
    }


def write(path: Path = SESSION_JSON) -> Path:
    """Build and write session.json deterministically. Returns the path."""
    session = build_session()
    # sort_keys + a trailing newline -> byte-identical output every run.
    path.write_text(json.dumps(session, indent=2, sort_keys=True) + "\n")
    return path


def main() -> int:
    out = write()
    print(f"Session written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
