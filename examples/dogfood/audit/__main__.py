"""Run the audit dogfood: two reconciliation agents, concurrent, attributed.

    python -m examples.dogfood.audit

This is the **real-agent run** (needs an Anthropic key in ``.env`` at the repo
root — see ``examples/dogfood``'s README). Two real agents run in parallel
threads against one on-disk ledger seeded with a genuine arithmetic imbalance,
under two ``client_id``s. Each must read the live amounts, compare them to the
expected values it is given, and book correcting adjustments to the suspense
account so the books balance. Afterwards we read the audit on the main thread
and print a per-``client_id`` compliance view, the corrected trial balance, and
the ledger state — proof that every adjustment is attributed, the books were
genuinely reconciled, and the source rows survived two concurrent reconcilers
without corruption. The offline ``tests/test_dogfood_audit.py`` is the *mechanism
test* (mocked client, deterministic, CI) — not a real agent.
"""

from __future__ import annotations

import os
import tempfile

from examples.dogfood.audit import (
    CLIENT_A,
    CLIENT_B,
    LEDGER_SCHEMA,
    compliance_view,
    default_tasks,
    ledger_balance,
    ledger_snapshot,
    run_two_agents,
)
from examples.dogfood.infra import scratch_sqlite

# Disjoint entry subsets so the common case is clean parallel work; if the model
# wanders onto the same entry the Abort policy catches the conflict at commit and
# that agent's run carries the IsolationConflict on ``AgentRun.error`` — surfaced
# in the view below. Each task hands the agent the expected (control) amounts and
# asks it to compute and book the corrections itself.
TASKS = default_tasks()


def _print_view(views: dict, runs: dict) -> None:
    print("=" * 72)
    print("Per-client compliance view (read from the audit AFTER both agents)")
    print("=" * 72)
    for cid, view in views.items():
        run = runs[cid].run
        err = f"  ERROR: {type(run.error).__name__}" if run.error else ""
        print(f"\nclient_id = {cid!r}  (final_state={run.final_state.name}){err}")
        print(f"  transactions attributed : {len(view.txns)}")
        print(f"  journalled effects       : {len(view.effects)}")
        for e in view.effects:
            print(f"    - {e['tool']:18s} status={e['status']}")
        print(f"  adjustments posted       : {len(view.adjustments)}")
        for a in view.adjustments:
            print(
                f"    - entry {a['entry_id']} delta={a['delta']} "
                f"reason={a['reason']!r}"
            )
        print(f"  discrepancies flagged    : {len(view.flags)}")
        for f in view.flags:
            print(f"    - entry {f['entry_id']} note={f['note']!r}")


def main() -> None:
    audit_fd, audit_path = tempfile.mkstemp(suffix=".audit.db", prefix="pherix_")
    os.close(audit_fd)
    try:
        with scratch_sqlite(schema=LEDGER_SCHEMA) as db:
            print("Ledger before reconciliation:")
            for row in ledger_snapshot(db):
                print(f"  entry {row['id']:>2}  {row['account']:12s} {row['amount']}")
            print("\nRunning two reconciliation agents concurrently...\n")

            runs = run_two_agents(db=db, audit_path=audit_path, tasks=TASKS)

            views = compliance_view(
                audit_path=audit_path,
                ledger_db=db,
                client_ids=[CLIENT_A, CLIENT_B],
            )
            _print_view(views, runs)

            print("\n" + "=" * 72)
            print("Ledger after reconciliation (source entries — uncorrupted):")
            print("=" * 72)
            for row in ledger_snapshot(db):
                print(f"  entry {row['id']:>2}  {row['account']:12s} {row['amount']}")

            balance = ledger_balance(db)
            verdict = "BALANCED" if balance == 0 else f"STILL OFF BY {balance}"
            print(
                f"\n  corrected trial balance (entries + adjustments) = {balance}"
                f"  -> {verdict}"
            )
            print(
                "\n! Two agents reconciled the same ledger in parallel against a "
                "genuine\n  imbalance. Every adjustment is attributed to its "
                "client_id in the audit\n  and in the row itself; the source "
                "entries are intact — isolation held.\n  Whether the books reach "
                "zero depends on what each agent actually computed,\n  not on a "
                "script. If both had raced on one entry row, the Abort policy\n  "
                "would have unwound the second committer.\n"
            )
    finally:
        os.unlink(audit_path)


if __name__ == "__main__":
    main()
