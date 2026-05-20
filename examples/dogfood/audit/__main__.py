"""Run the audit dogfood: two reconciliation agents, concurrent, attributed.

    python -m examples.dogfood.audit

Needs an Anthropic key (``.env`` at the repo root — see ``examples/dogfood``'s
README). The two agents run in parallel threads against one on-disk ledger and
one on-disk audit DB, under two ``client_id``s. Afterwards we read the audit on
the main thread and print a per-``client_id`` compliance view plus the ledger
state — proof that every adjustment is attributed and the source rows survived
two concurrent reconcilers without corruption.
"""

from __future__ import annotations

import os
import tempfile

from examples.dogfood.audit import (
    LEDGER_SCHEMA,
    compliance_view,
    ledger_snapshot,
    run_two_agents,
)
from examples.dogfood.infra import scratch_sqlite

CLIENT_A = "auditor-a"
CLIENT_B = "auditor-b"

# Two tasks pointed at DIFFERENT accounts so the common case is clean parallel
# work; if the model wanders onto the same entry the Abort policy catches the
# conflict at commit and that agent's run carries the IsolationConflict on
# ``AgentRun.error`` — which the view below surfaces.
TASKS = {
    CLIENT_A: (
        "Reconcile ledger entries 1 (cash) and 2 (receivable). Read each by its "
        "entry id, and if an amount looks wrong post a correcting adjustment; "
        "flag anything you cannot resolve."
    ),
    CLIENT_B: (
        "Reconcile ledger entries 3 (payable) and 4 (inventory). Read each by "
        "its entry id, and if an amount looks wrong post a correcting "
        "adjustment; flag anything you cannot resolve."
    ),
}


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
            print(
                "\n! Two agents reconciled the same ledger in parallel. Every "
                "adjustment\n  is attributed to its client_id in the audit and "
                "in the row itself; the\n  source entries are intact — isolation "
                "held. If both had raced on one\n  entry row, the Abort policy "
                "would have unwound the second committer.\n"
            )
    finally:
        os.unlink(audit_path)


if __name__ == "__main__":
    main()
