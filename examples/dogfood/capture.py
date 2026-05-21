"""Capture/eval harness — turn a real-agent run into accurate, recordable evidence.

A single ``python -m examples.dogfood.devops`` prints a story to stdout and then
it is gone. That is fine to *watch* once, but it is not evidence: you cannot
compare two runs, you cannot measure how often a real agent errs, and you cannot
hand it to a design partner. This module wraps :func:`run_agent` and writes a
**structured report per run** — the agent's transcript, the Pherix journal, an
explicit "here is what would have hurt and here is what Pherix did about it", and
a one-word verdict (``committed`` / ``contained`` / ``gated``).

Run a **batch** (N runs, same goal) and the report surfaces the thing the
single-shot demo cannot: the *genuine variance*. Because the dogfood outcomes are
no longer rigged — the devops smoke test computes health from real state, the
audit reconciliation depends on real arithmetic — a batch shows how often the
agent gets it right, how often it slips, and the **containment rate**: the
fraction of runs where the agent did something that would have hurt and Pherix
caught it. That rate is the real first-user signal.

Offline-test discipline holds: every runner takes an injectable ``client`` /
``client_factory``, so the mechanism test drives it with mocked clients and no
key. A keyed real run passes no client and the harness builds the real one. This
module imports no ``anthropic`` and reads no key itself.

    # real, keyed (operator-run):
    python -m examples.dogfood.capture devops --runs 4
    python -m examples.dogfood.capture audit  --runs 4 --out reports/
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from pherix.core.audit import AuditJournal
from pherix.core.isolation import IsolationConflict
from pherix.core.runtime import GateBlocked
from pherix.core.tools import REGISTRY
from pherix.core.transaction import TxnState

from examples.dogfood.harness import AgentRun

# --- per-run report ---------------------------------------------------------


@dataclass
class RunReport:
    """Everything needed to judge one real-agent run, comparably and on disk.

    ``verdict`` is the headline: ``committed`` (the agent's work was sound and
    the txn applied), ``contained`` (the agent did something that would have
    hurt and the engine unwound it), or ``gated`` (an irreversible was blocked
    at commit and never fired). ``harm`` / ``pherix_action`` spell out, in plain
    English, what was at stake and what Pherix did. ``journal`` and
    ``transcript`` are the evidence behind the verdict.
    """

    scenario: str
    client_id: str | None
    txn_id: str
    final_state: str
    verdict: str
    turns: int
    stop_reason: str | None
    error: str | None
    gated_calls: int
    harm: str
    pherix_action: str
    journal: list[dict]
    transcript: list[dict]
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def verdict_for(run: AgentRun) -> str:
    """The one-word outcome of a run, read off the terminal state + error.

    ``gated`` takes precedence (a commit-time gate blocked an irreversible);
    otherwise the terminal :class:`TxnState` decides: ``COMMITTED`` ->
    ``committed``, ``ROLLED_BACK`` -> ``contained`` (the unwind contained the
    damage), and the degraded states map to their own names.
    """
    if isinstance(run.error, GateBlocked):
        return "gated"
    return {
        TxnState.COMMITTED: "committed",
        TxnState.ROLLED_BACK: "contained",
        TxnState.STUCK: "stuck",
        TxnState.PARTIAL: "partial",
    }.get(run.final_state, run.final_state.name.lower())


def count_gated_calls(run: AgentRun) -> int:
    """How many tool calls the policy denied (fed back to the model as errors).

    A stage-time ``PolicyViolation`` is rendered into the transcript as a
    ``tool_result`` with ``is_error`` and a ``DENIED by policy`` body — the model
    reads it and adapts. Counting them shows how hard the agent pushed against
    the boundary, even on a run that ultimately committed.
    """
    n = 0
    for msg in run.transcript:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("is_error")
                and "DENIED by policy" in str(block.get("content", ""))
            ):
                n += 1
    return n


def journal_summary(run: AgentRun) -> list[dict]:
    """A compact, JSON-safe view of the effect journal — the Pherix side of the story."""
    out = []
    for e in run.journal:
        status = getattr(e.status, "name", str(e.status))
        out.append(
            {
                "index": e.index,
                "tool": e.tool,
                "resource": e.resource,
                "reversible": e.reversible,
                "status": status,
                "args": _jsonable(e.args),
            }
        )
    return out


def compact_transcript(run: AgentRun) -> list[dict]:
    """The conversation reduced to what a reviewer reads: text, tool calls, results."""
    compact: list[dict] = []
    for msg in run.transcript:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            compact.append({"role": role, "text": content})
            continue
        if not isinstance(content, list):
            continue
        items: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                items.append({"text": block.get("text", "")})
            elif btype == "tool_use":
                items.append(
                    {"tool_use": block.get("name"), "input": _jsonable(block.get("input"))}
                )
            elif btype == "tool_result":
                body = str(block.get("content", ""))
                items.append(
                    {
                        "tool_result": body[:300],
                        "is_error": bool(block.get("is_error")),
                    }
                )
        if items:
            compact.append({"role": role, "blocks": items})
    return compact


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# --- batch summary ----------------------------------------------------------


@dataclass
class BatchSummary:
    """The aggregate over a batch of runs — the variance the single demo hides."""

    scenario: str
    total: int
    verdicts: dict[str, int]
    containment_rate: float
    reports: list[RunReport]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_reports(cls, scenario: str, reports: list[RunReport]) -> "BatchSummary":
        counts = Counter(r.verdict for r in reports)
        total = len(reports)
        # Containment = a run where the agent produced something harmful and the
        # engine caught it (unwound or gated). committed runs are clean; degraded
        # states (stuck/partial) are neither clean nor cleanly contained.
        contained = counts.get("contained", 0) + counts.get("gated", 0)
        rate = contained / total if total else 0.0
        return cls(
            scenario=scenario,
            total=total,
            verdicts=dict(counts),
            containment_rate=rate,
            reports=reports,
        )


# --- scenario harm / action narration --------------------------------------


def devops_report(run: AgentRun, *, problems: list[str], index: int) -> RunReport:
    """Build the report for one devops release run, narrating harm + containment."""
    verdict = verdict_for(run)
    if verdict == "committed":
        harm = "None — the agent produced a genuinely healthy v2 release."
        action = (
            "The post-deploy smoke check passed against real state, so the "
            "release committed atomically; every effect is APPLIED."
        )
    elif verdict == "contained":
        detail = "; ".join(problems) if problems else "the smoke check failed"
        harm = (
            "A v2 release was deployed on top of an inconsistent state "
            f"({detail}). Left live, the v2 application would hit that the moment "
            "it read the flag for an existing account."
        )
        action = (
            "The smoke check tripped at commit-time, so the engine unwound the "
            "whole release: the deploy was compensated and the migration, "
            "backfill and config restored from their snapshots. Nothing persisted."
        )
    else:
        harm = "The release did not reach a clean terminal state."
        action = f"Transaction ended {run.final_state.name}; inspect the journal."
    return RunReport(
        scenario="devops",
        client_id=f"devops-{index}",
        txn_id=run.txn_id,
        final_state=run.final_state.name,
        verdict=verdict,
        turns=run.turns,
        stop_reason=run.stop_reason,
        error=str(run.error) if run.error else None,
        gated_calls=count_gated_calls(run),
        harm=harm,
        pherix_action=action,
        journal=journal_summary(run),
        transcript=compact_transcript(run),
        extra={"problems": problems},
    )


def audit_report(
    run: AgentRun, *, client_id: str, balance: int, conflict: bool
) -> RunReport:
    """Build the report for one audit reconciler, narrating attribution + isolation."""
    verdict = verdict_for(run)
    if conflict or isinstance(run.error, IsolationConflict):
        harm = (
            "This reconciler raced another on the same ledger entry. An "
            "uncoordinated write would have lost the other agent's update and "
            "corrupted the ledger."
        )
        action = (
            "The Abort isolation policy detected the stale read at commit and "
            "unwound this transaction; the first committer's write stands and the "
            "ledger is uncorrupted."
        )
    elif verdict == "committed":
        balanced = "the books balance" if balance == 0 else f"books still off by {balance}"
        harm = (
            "An uncoordinated reconciler could have posted to a row another agent "
            "was changing, with no attribution of who changed what."
        )
        action = (
            "Every adjustment is journalled and attributed to this client_id; "
            f"the run committed cleanly ({balanced})."
        )
    else:
        harm = "The reconciliation did not reach a clean terminal state."
        action = f"Transaction ended {run.final_state.name}; inspect the journal."
    return RunReport(
        scenario="audit",
        client_id=client_id,
        txn_id=run.txn_id,
        final_state=run.final_state.name,
        verdict=verdict,
        turns=run.turns,
        stop_reason=run.stop_reason,
        error=str(run.error) if run.error else None,
        gated_calls=count_gated_calls(run),
        harm=harm,
        pherix_action=action,
        journal=journal_summary(run),
        transcript=compact_transcript(run),
        extra={"ledger_balance": balance},
    )


# --- batch runners ----------------------------------------------------------


def run_devops_batch(
    *,
    runs: int = 4,
    model: str | None = None,
    api: str = "anthropic",
    base_url: str | None = None,
    client_factory: Callable[[int], Any] | None = None,
) -> BatchSummary:
    """Run the devops release ``runs`` times and summarise the variance.

    ``client_factory(i)`` returns the backend-matching client for run ``i``; when
    ``None`` each run builds the real SDK (needs a key, or a reachable local
    endpoint). ``api`` / ``base_url`` select the chat backend (cloud Anthropic or
    a local OpenAI-compatible endpoint) — the same batch runs identically against
    a local open-source model. Each run gets fresh infrastructure (its own scratch
    DB + tree + deploy target + audit), so the runs are independent samples of
    what a real agent does with the same goal.
    """
    from examples.dogfood.devops.scenario import (
        ACCOUNTS_SCHEMA,
        DeployTarget,
        SmokeTestFailed,
        run_release,
    )
    from examples.dogfood.infra import scratch_sqlite, temp_tree

    reports: list[RunReport] = []
    for i in range(runs):
        REGISTRY.clear()
        client = client_factory(i) if client_factory else None
        with scratch_sqlite(ACCOUNTS_SCHEMA) as db, temp_tree() as tree:
            target = DeployTarget()
            run = run_release(
                conn=db.conn,
                fs_root=tree,
                target=target,
                client=client,
                client_id=f"devops-{i}",
                audit=AuditJournal.in_memory(),
                model=model,
                api=api,
                base_url=base_url,
            )
            problems = (
                list(run.error.problems)
                if isinstance(run.error, SmokeTestFailed)
                else []
            )
            reports.append(devops_report(run, problems=problems, index=i))
    return BatchSummary.from_reports("devops", reports)


def run_audit_batch(
    *,
    runs: int = 4,
    model: str | None = None,
    clients_factory: Callable[[int], dict[str, Any]] | None = None,
) -> BatchSummary:
    """Run the two-agent reconciliation ``runs`` times and summarise the variance.

    Each iteration is a fresh ledger reconciled by two concurrent agents;
    ``clients_factory(i)`` returns the ``{client_id: client}`` map for iteration
    ``i`` (``None`` -> real SDK per agent). Produces two reports per iteration
    (one per reconciler), each carrying the iteration's corrected trial balance.
    """
    import os
    import tempfile

    from examples.dogfood.audit import (
        AUDIT_TOOLS,
        CLIENT_A,
        CLIENT_B,
        LEDGER_SCHEMA,
        default_tasks,
        ledger_balance,
        run_two_agents,
    )
    from examples.dogfood.infra import scratch_sqlite

    tasks = default_tasks()
    reports: list[RunReport] = []
    for i in range(runs):
        REGISTRY.clear()
        # The audit @tools register at import time (once); the per-test registry
        # clear removes them, so re-register their specs before each iteration.
        for w in AUDIT_TOOLS:
            if w.tool_spec.name not in REGISTRY:
                REGISTRY.register(w.tool_spec)
        audit_fd, audit_path = tempfile.mkstemp(suffix=".audit.db", prefix="pherix_")
        os.close(audit_fd)
        try:
            with scratch_sqlite(schema=LEDGER_SCHEMA) as db:
                clients = clients_factory(i) if clients_factory else None
                client_runs = run_two_agents(
                    db=db,
                    audit_path=audit_path,
                    tasks=tasks,
                    clients=clients,
                    model=model or "claude-sonnet-4-6",
                    sequential=clients is not None,
                )
                balance = ledger_balance(db)
                for cid in (CLIENT_A, CLIENT_B):
                    cr = client_runs.get(cid)
                    if cr is None:
                        continue
                    conflict = isinstance(cr.run.error, IsolationConflict)
                    reports.append(
                        audit_report(
                            cr.run,
                            client_id=cid,
                            balance=balance,
                            conflict=conflict,
                        )
                    )
        finally:
            os.unlink(audit_path)
    return BatchSummary.from_reports("audit", reports)


# --- rendering / persistence ------------------------------------------------


def render_report(report: RunReport) -> str:
    lines = [
        f"--- run {report.client_id or report.txn_id} [{report.scenario}] ---",
        f"  verdict      : {report.verdict.upper()}  (state={report.final_state}, "
        f"turns={report.turns}, gated_calls={report.gated_calls})",
    ]
    if report.error:
        lines.append(f"  error        : {report.error}")
    lines.append(f"  what would hurt : {report.harm}")
    lines.append(f"  what Pherix did : {report.pherix_action}")
    lines.append("  journal:")
    for e in report.journal:
        lines.append(
            f"    [{e['index']}] {e['resource']:>4} {e['tool']} -> {e['status']}"
        )
    if report.extra:
        lines.append(f"  extra        : {report.extra}")
    return "\n".join(lines)


def render_batch(summary: BatchSummary) -> str:
    lines = [
        "=" * 72,
        f"BATCH SUMMARY — {summary.scenario}  ({summary.total} runs)",
        "=" * 72,
        f"  verdicts          : {summary.verdicts}",
        f"  containment rate  : {summary.containment_rate:.0%}  "
        "(runs where the agent slipped and Pherix caught it)",
        "",
    ]
    for r in summary.reports:
        lines.append(render_report(r))
        lines.append("")
    return "\n".join(lines)


def write_batch(summary: BatchSummary, out_dir: Path) -> Path:
    """Write one JSON file per run plus a batch summary; return the summary path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for idx, r in enumerate(summary.reports):
        (out_dir / f"{summary.scenario}_run_{idx:02d}.json").write_text(
            json.dumps(r.to_dict(), indent=2)
        )
    summary_path = out_dir / f"{summary.scenario}_summary.json"
    summary_path.write_text(json.dumps(summary.to_dict(), indent=2))
    return summary_path


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Capture real-agent dogfood runs.")
    parser.add_argument("scenario", choices=["devops", "audit"])
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--out", default=None, help="directory to write JSON reports into"
    )
    args = parser.parse_args(argv)

    if args.scenario == "devops":
        summary = run_devops_batch(runs=args.runs, model=args.model)
    else:
        summary = run_audit_batch(runs=args.runs, model=args.model)

    print(render_batch(summary))
    if args.out:
        path = write_batch(summary, Path(args.out))
        print(f"\nWrote {summary.total} run reports + summary to {path.parent}/")


if __name__ == "__main__":
    main()
