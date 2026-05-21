"""Seed a representative audit journal for the inspector — fixtures + demo.

There are no live demos to point at yet, so the inspector ships its own
journal generator. It writes, through the real :class:`AuditJournal`, one
transaction per story the console must render unmistakably:

  * a clean reversible commit (the happy path),
  * a rollback — every effect COMPENSATED, txn ROLLED_BACK (the backward fold),
  * a gated irreversible held at the gate (the policy boundary),
  * a STUCK transaction — a compensator went missing mid-unwind,
  * a dry-run (``dry_run=1``) that touched nothing,
  * two attributed transactions under different ``client_id``s (the audit view).

Each is built from genuine ``Transaction`` / ``Effect`` objects in their
terminal state, so the rows are byte-for-byte what the engine would have
written — the inspector can't tell a seeded journal from a real one. Run::

    python -m pherix.inspector.seed demo.db

then point the console at ``demo.db``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.transaction import Transaction, TxnState


def _ts(minutes_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def _effect(
    txn_id: str,
    idx: int,
    tool: str,
    resource: str,
    reversible: bool,
    status: EffectStatus,
    *,
    args: dict | None = None,
    result: object = None,
    read_keys: list | None = None,
    write_keys: list | None = None,
    compensator: str | None = None,
    minutes_ago: float = 0.0,
) -> Effect:
    return Effect(
        txn_id=txn_id,
        index=idx,
        tool=tool,
        args=args or {},
        resource=resource,
        reversible=reversible,
        status=status,
        result=result,
        read_keys=read_keys or [],
        write_keys=write_keys or [],
        compensator=compensator,
        ts=_ts(minutes_ago),
    )


def _write(journal: AuditJournal, txn: Transaction, **meta) -> None:
    """Persist a fully-formed transaction and its effects in terminal state."""
    journal.record_transaction(txn, **meta)
    for eff in txn.effects:
        journal.record_effect(eff)


# --- the six stories --------------------------------------------------------


def _clean_commit() -> Transaction:
    """DevOps-style atomic release: read config, two SQL writes, all applied."""
    t = Transaction(txn_id="txn-clean-deploy01", state=TxnState.COMMITTED)
    t.effects = [
        _effect(t.txn_id, 0, "read_release", "sql", True, EffectStatus.APPLIED,
                args={"name": "v2.4.0"}, result={"prev": "v2.3.9"},
                read_keys=[["sql", ["releases", "current"], 11]], minutes_ago=9.4),
        _effect(t.txn_id, 1, "bump_version", "sql", True, EffectStatus.APPLIED,
                args={"to": "v2.4.0"}, write_keys=[["sql", ["releases", "current"], 12]],
                minutes_ago=9.3),
        _effect(t.txn_id, 2, "write_manifest", "fs", True, EffectStatus.APPLIED,
                args={"path": "deploy/manifest.json"},
                write_keys=[["fs", ["deploy/manifest.json"]]], minutes_ago=9.2),
    ]
    return t


def _rollback() -> Transaction:
    """A release that failed its smoke test — the whole txn unwinds."""
    t = Transaction(txn_id="txn-rollback-rel02", state=TxnState.ROLLED_BACK)
    t.effects = [
        _effect(t.txn_id, 0, "bump_version", "sql", True, EffectStatus.COMPENSATED,
                args={"to": "v2.5.0"}, write_keys=[["sql", ["releases", "current"], 13]],
                minutes_ago=7.0),
        _effect(t.txn_id, 1, "write_manifest", "fs", True, EffectStatus.COMPENSATED,
                args={"path": "deploy/manifest.json"},
                write_keys=[["fs", ["deploy/manifest.json"]]], minutes_ago=6.9),
    ]
    return t


def _gated() -> Transaction:
    """An irreversible charge with no compensator — held at the gate."""
    t = Transaction(txn_id="txn-gated-charge03", state=TxnState.STAGED)
    t.effects = [
        _effect(t.txn_id, 0, "read_invoice", "sql", True, EffectStatus.APPLIED,
                args={"invoice": "INV-7781"}, result={"amount": 4200},
                read_keys=[["sql", ["invoices", "INV-7781"], 3]], minutes_ago=3.1),
        _effect(t.txn_id, 1, "charge_card", "http", False, EffectStatus.GATED,
                args={"invoice": "INV-7781", "amount": 4200},
                minutes_ago=3.0),
    ]
    return t


def _stuck() -> Transaction:
    """Mid-fire failure, then a missing compensator — operator must intervene."""
    t = Transaction(txn_id="txn-stuck-payout04", state=TxnState.STUCK)
    t.effects = [
        _effect(t.txn_id, 0, "debit_ledger", "sql", True, EffectStatus.COMPENSATED,
                args={"account": "acct-19", "amount": 900},
                write_keys=[["sql", ["ledger", "acct-19"], 41]], minutes_ago=2.2),
        _effect(t.txn_id, 1, "send_payout", "http", False, EffectStatus.APPLIED,
                args={"to": "vendor-3", "amount": 900},
                result={"payout_id": "po_8810"}, compensator="reverse_payout",
                minutes_ago=2.1),
        _effect(t.txn_id, 2, "notify_vendor", "http", False, EffectStatus.FAILED,
                args={"to": "vendor-3"}, minutes_ago=2.0),
    ]
    return t


def _dry_run() -> Transaction:
    """A speculative dry-run — verdicts captured, world untouched."""
    t = Transaction(txn_id="txn-dryrun-plan05", state=TxnState.COMMITTED)
    t.effects = [
        _effect(t.txn_id, 0, "read_budget", "sql", True, EffectStatus.APPLIED,
                args={"team": "growth"}, result={"remaining": 1500},
                read_keys=[["sql", ["budgets", "growth"], 7]], minutes_ago=1.3),
        _effect(t.txn_id, 1, "charge_card", "http", False, EffectStatus.STAGED,
                args={"team": "growth", "amount": 2000}, minutes_ago=1.2),
    ]
    return t


def _attributed() -> list[Transaction]:
    """Two clients hitting the same gateway — provenance in the audit view."""
    a = Transaction(txn_id="txn-clientA-q06", state=TxnState.COMMITTED)
    a.effects = [
        _effect(a.txn_id, 0, "query_orders", "sql", True, EffectStatus.APPLIED,
                args={"status": "open"}, read_keys=[["sql", ["orders", "*"], 88]],
                minutes_ago=0.6),
    ]
    b = Transaction(txn_id="txn-clientB-w07", state=TxnState.COMMITTED)
    b.effects = [
        _effect(b.txn_id, 0, "update_order", "sql", True, EffectStatus.APPLIED,
                args={"order": "ord-552", "status": "shipped"},
                write_keys=[["sql", ["orders", "ord-552"], 89]], minutes_ago=0.4),
    ]
    return [a, b]


def _verdict(effect_index: int, phase: str, allow: bool, kind: str,
             rule_name: str | None, reason: str | None = None) -> dict:
    return {
        "effect_index": effect_index, "phase": phase, "allow": allow,
        "kind": kind, "rule_name": rule_name, "reason": reason,
    }


def seed_demo_journal(path: str) -> dict:
    """Write the full demo journal to ``path`` (created if absent). Idempotent
    only if ``path`` is fresh — re-seeding an existing journal will raise on
    the duplicate primary keys, which is the honest behaviour for an
    append-only log."""
    journal = AuditJournal(path)
    try:
        _write(journal, _clean_commit())
        _write(journal, _rollback())
        _write(journal, _gated())
        # The gated charge: a spend cap allows it at stage but the running
        # total trips it at commit — the world-state divergence the inspector
        # surfaces (allowed when planned, denied when it fired).
        journal.record_verdicts("txn-gated-charge03", [
            _verdict(0, "stage", True, "rule", "read_only_ok"),
            _verdict(1, "stage", True, "cap", "Cap.sum(charge_card, max=5000)"),
            _verdict(0, "commit", True, "rule", "read_only_ok"),
            _verdict(1, "commit", False, "cap", "Cap.sum(charge_card, max=5000)",
                     "would exceed sum cap (max=5000): running=4200, "
                     "contribution=4200"),
        ])
        _write(journal, _stuck())
        _write(journal, _dry_run(), dry_run=True)
        # The dry-run captured both phases without firing — the speculative view.
        journal.record_verdicts("txn-dryrun-plan05", [
            _verdict(0, "stage", True, "rule", "budget_guard"),
            _verdict(1, "stage", False, "rule", "budget_guard",
                     "charge 2000 exceeds remaining budget 1500"),
            _verdict(0, "commit", True, "rule", "budget_guard"),
            _verdict(1, "commit", False, "rule", "budget_guard",
                     "charge 2000 exceeds remaining budget 1500"),
        ])
        attributed = _attributed()
        _write(journal, attributed[0], client_id="claude-code")
        _write(journal, attributed[1], client_id="cursor-agent")
        return {
            "path": path,
            "transactions": 7,
            "stories": [
                "clean commit", "rollback", "gated irreversible",
                "STUCK", "dry-run", "attributed (claude-code)",
                "attributed (cursor-agent)",
            ],
        }
    finally:
        journal.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m pherix.inspector.seed <path-to-new-journal.db>")
        return 2
    summary = seed_demo_journal(argv[0])
    print(f"seeded {summary['transactions']} transactions into {summary['path']}:")
    for s in summary["stories"]:
        print(f"  · {s}")
    print(f"\nrun the console:  python -m pherix.inspector --db {summary['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
