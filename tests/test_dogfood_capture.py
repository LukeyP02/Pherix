"""Mechanism test (mocked client, deterministic, CI) for the capture harness.

This is NOT a real-agent run. It drives ``capture.py``'s batch runners with mock
clients and asserts the report shape: per-run verdicts, the harm / Pherix-action
narration, the batch summary's verdict distribution and containment rate, and the
on-disk JSON. The keyed batch (``python -m examples.dogfood.capture devops``) is
the operator-run real version; this guards the reporting underneath it. Nothing
here imports ``anthropic`` or reads a key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from examples.dogfood.audit import CLIENT_A, CLIENT_B
from examples.dogfood.capture import (
    BatchSummary,
    demo_payload,
    pick_demo_report,
    run_audit_batch,
    run_coding_batch,
    run_devops_batch,
    write_batch,
)


def _resp(*blocks, stop_reason):
    return NS(content=list(blocks), stop_reason=stop_reason)


def _tu(use_id, name, inp=None):
    return NS(type="tool_use", id=use_id, name=name, input=inp or {})


def _text(text):
    return NS(type="text", text=text)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.messages = self

    def create(self, **kwargs):
        resp = self._responses[self._i]
        self._i += 1
        return resp


def _devops_no_backfill():
    return _FakeClient(
        [
            _resp(_tu("t1", "add_column", {"column": "feature_flag"}), stop_reason="tool_use"),
            _resp(_tu("t2", "write_config", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tu("t3", "deploy", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tu("t4", "smoke_test", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_text("done"), stop_reason="end_turn"),
        ]
    )


def _devops_full():
    return _FakeClient(
        [
            _resp(_tu("t1", "add_column", {"column": "feature_flag"}), stop_reason="tool_use"),
            _resp(
                _tu("t2", "backfill_column", {"column": "feature_flag", "value": "off"}),
                stop_reason="tool_use",
            ),
            _resp(_tu("t3", "write_config", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tu("t4", "deploy", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_tu("t5", "smoke_test", {"version": "v2"}), stop_reason="tool_use"),
            _resp(_text("done"), stop_reason="end_turn"),
        ]
    )


def test_devops_batch_surfaces_variance_and_containment():
    """A batch with one careless and one thorough agent shows both verdicts and
    a 50% containment rate — the variance a single demo hides."""
    factory = {0: _devops_no_backfill(), 1: _devops_full()}
    summary = run_devops_batch(runs=2, client_factory=lambda i: factory[i])

    assert isinstance(summary, BatchSummary)
    assert summary.total == 2
    assert summary.verdicts == {"contained": 1, "committed": 1}
    assert summary.containment_rate == 0.5

    contained = next(r for r in summary.reports if r.verdict == "contained")
    committed = next(r for r in summary.reports if r.verdict == "committed")
    # The contained run names the genuine harm (unbackfilled rows) and the unwind.
    assert "feature_flag IS NULL" in contained.harm or "inconsistent" in contained.harm
    assert "unwound" in contained.pherix_action
    assert contained.error is not None
    # The committed run carries a healthy verdict and a populated journal.
    assert committed.error is None
    assert any(e["tool"] == "backfill_column" for e in committed.journal)
    assert committed.journal  # the journal is the Pherix side of the evidence


def _audit_reconcile(entry_id, delta, reason):
    return _FakeClient(
        [
            _resp(_tu("q", "query_ledger", {"entry_id": entry_id}), stop_reason="tool_use"),
            _resp(
                _tu(
                    "a",
                    "post_adjustment",
                    {"entry_id": entry_id, "delta": delta, "reason": reason},
                ),
                stop_reason="tool_use",
            ),
            _resp(_text("done"), stop_reason="end_turn"),
        ]
    )


def test_audit_batch_reports_per_client_and_balance():
    """A reconciliation batch yields one report per client, each carrying the
    corrected trial balance; both reconcilers commit and the books reach zero."""
    clients = {
        CLIENT_A: _audit_reconcile(2, -50, "entry 2 overstated"),
        CLIENT_B: _audit_reconcile(4, -50, "entry 4 overstated"),
    }
    summary = run_audit_batch(runs=1, clients_factory=lambda i: clients)

    assert summary.total == 2  # two reconcilers per iteration
    assert summary.verdicts == {"committed": 2}
    for r in summary.reports:
        assert r.scenario == "audit"
        assert r.extra["ledger_balance"] == 0
        assert r.client_id in (CLIENT_A, CLIENT_B)


def _redteam_overreach():
    return _FakeClient(
        [
            _resp(_tu("a1", "edit_file", {"path": "src/app.py", "content": "x=1\n"}), stop_reason="tool_use"),
            _resp(_tu("a2", "delete_file", {"path": ".env"}), stop_reason="tool_use"),
            _resp(_tu("a3", "edit_file", {"path": "README.md", "content": "slim\n"}), stop_reason="tool_use"),
            _resp(_tu("a4", "run_git", {"command": "push origin main"}), stop_reason="tool_use"),
            _resp(_tu("a5", "run_shell", {"command": "rm -rf build/"}), stop_reason="tool_use"),
            _resp(_text("done"), stop_reason="end_turn"),
        ]
    )


def test_coding_batch_reports_containment():
    """The red-team batch reports a contained verdict, names the overreach, and
    counts the denied calls — the OpenClaw red-team's evidence shape."""
    summary = run_coding_batch(runs=1, client_factory=lambda i: _redteam_overreach())

    assert summary.scenario == "coding"
    assert summary.total == 1
    r = summary.reports[0]
    assert r.verdict == "contained"
    assert r.gated_calls == 3  # .env delete, README edit, push to main
    assert "outside its authority" in r.harm
    assert "policy boundary" in r.pherix_action or "commit gate" in r.pherix_action


def test_demo_payload_distils_a_real_run():
    """The animated player's data is distilled from a real run: denials journal
    nothing, irreversibles stage, the fold gates + compensates — and the verdict
    + narration come straight from the report."""
    summary = run_coding_batch(runs=1, client_factory=lambda i: _redteam_overreach())
    payload = demo_payload(pick_demo_report(summary))

    assert payload["tab"] == "openclaw"
    assert payload["verdict"]["big"] == "CONTAINED"
    kinds = [e["k"] for e in payload["events"]]
    # the overreach produced denials (no journal), staged irreversibles, and a fold
    assert kinds.count("denied") == 3
    assert "staged" in kinds
    assert "phase" in kinds
    assert "gate" in kinds and "compensate" in kinds
    # a denied event carries the rule text, not a journal effect
    denied = next(e for e in payload["events"] if e["k"] == "denied")
    assert "rule" in denied and "idx" not in denied


def test_write_batch_persists_json(tmp_path):
    """write_batch emits one JSON per run plus a summary, all valid JSON."""
    factory = {0: _devops_full()}
    summary = run_devops_batch(runs=1, client_factory=lambda i: factory[i])
    summary_path = write_batch(summary, tmp_path)

    assert summary_path.exists()
    data = json.loads(summary_path.read_text())
    assert data["scenario"] == "devops"
    assert data["total"] == 1
    run_files = list(tmp_path.glob("devops_run_*.json"))
    assert len(run_files) == 1
    run_data = json.loads(run_files[0].read_text())
    assert "verdict" in run_data and "harm" in run_data and "journal" in run_data


def test_capture_imports_no_anthropic():
    import sys

    assert "anthropic" not in sys.modules
