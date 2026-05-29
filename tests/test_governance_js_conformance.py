"""The load-bearing pin: the browser's JS verdict mirror == the Python engine.

The governance UI shows live verdicts client-side via ``site/policy-eval.js``.
That file is a re-implementation of ``Policy.collect_verdicts`` and could
silently drift from the engine. This test forecloses that: it runs a battery of
``(spec, scenario, world)`` cases through *both* the Python engine
(``pherix.governance.preview.preview``) and the JS mirror (under Node), and
asserts they agree verdict-for-verdict — same disposition per effect, same
``(effect_index, allow, rule_name)`` per verdict, same ``is_clean``.

Skipped (not failed) if Node is unavailable, so the offline Python suite stays
green on a machine without it. CI / the dev box has Node, so the pin holds where
it matters.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pherix.governance.preview import preview
from pherix.governance.spec import PolicySpec

NODE = shutil.which("node")
SITE = Path(__file__).resolve().parent.parent / "site" / "policy-eval.js"
RUNNER = Path(__file__).resolve().parent / "governance_js_runner.cjs"

pytestmark = pytest.mark.skipif(NODE is None, reason="Node not installed")


# -- the battery -------------------------------------------------------------
# Each case: a spec dict, a scenario (list of effect dicts), and an optional
# world map. Chosen to hit every branch: allow/deny lists, count + sum caps
# (including accumulation across the journal), the gate, world-state rules
# (paid / not-paid / absent), and combinations.

CASES = [
    {
        "name": "allow-list denies unlisted",
        "spec": PolicySpec(name="ro", allow=["read_file"]).to_dict(),
        "scenario": [
            {"tool": "read_file", "args": {}},
            {"tool": "write_file", "args": {}},
        ],
        "world": [],
    },
    {
        "name": "deny-list",
        "spec": PolicySpec(name="d", deny=["drop_table"]).to_dict(),
        "scenario": [{"tool": "drop_table", "args": {}}],
        "world": [],
    },
    {
        "name": "count cap trips third",
        "spec": PolicySpec(
            name="c",
            caps=[{"kind": "count", "tool": "send", "max": 2}],
        ).to_dict(),
        "scenario": [{"tool": "send", "args": {}} for _ in range(4)],
        "world": [],
    },
    {
        "name": "sum cap accumulates",
        "spec": PolicySpec(
            name="c",
            caps=[{"kind": "sum", "tool": "charge", "field": "amount", "max": 100}],
        ).to_dict(),
        "scenario": [
            {"tool": "charge", "args": {"amount": 60}},
            {"tool": "charge", "args": {"amount": 60}},
            {"tool": "charge", "args": {}},  # missing field → contributes 0
        ],
        "world": [],
    },
    {
        "name": "gate irreversible",
        "spec": PolicySpec(name="g", gate_irreversibles=True).to_dict(),
        "scenario": [
            {"tool": "webhook", "args": {}, "reversible": False},
            {"tool": "charge", "args": {}, "reversible": False, "compensator": "refund"},
        ],
        "world": [],
    },
    {
        "name": "gate off",
        "spec": PolicySpec(name="g", gate_irreversibles=False).to_dict(),
        "scenario": [{"tool": "webhook", "args": {}, "reversible": False}],
        "world": [],
    },
    {
        "name": "refund paid → allow",
        "spec": PolicySpec(
            name="r",
            rules=[{"template": "refund_if_paid", "params": {"tool": "refund_order"}}],
        ).to_dict(),
        "scenario": [{"tool": "refund_order", "args": {"order_id": 42}}],
        "world": [
            {"resource": "sql", "key": ["orders", "id", 42, "status"], "value": "paid"}
        ],
    },
    {
        "name": "refund unpaid → deny",
        "spec": PolicySpec(
            name="r",
            rules=[{"template": "refund_if_paid", "params": {"tool": "refund_order"}}],
        ).to_dict(),
        "scenario": [{"tool": "refund_order", "args": {"order_id": 42}}],
        "world": [
            {"resource": "sql", "key": ["orders", "id", 42, "status"], "value": "void"}
        ],
    },
    {
        "name": "refund absent → deny",
        "spec": PolicySpec(
            name="r",
            rules=[{"template": "refund_if_paid", "params": {"tool": "refund_order"}}],
        ).to_dict(),
        "scenario": [{"tool": "refund_order", "args": {"order_id": 7}}],
        "world": [],
    },
    {
        "name": "arg_equals_denied with value",
        "spec": PolicySpec(
            name="a",
            rules=[
                {
                    "template": "arg_equals_denied",
                    "params": {"tool": "update", "arg": "tier", "value": "enterprise"},
                }
            ],
        ).to_dict(),
        "scenario": [
            {"tool": "update", "args": {"tier": "enterprise"}},
            {"tool": "update", "args": {"tier": "free"}},
            {"tool": "other", "args": {}},
        ],
        "world": [],
    },
    {
        "name": "everything at once",
        "spec": PolicySpec(
            name="mix",
            allow=["charge", "refund_order", "send", "webhook"],
            deny=["drop_table"],
            caps=[
                {"kind": "sum", "tool": "charge", "field": "amount", "max": 1000},
                {"kind": "count", "tool": "send", "max": 1},
            ],
            rules=[
                {"template": "refund_if_paid", "params": {"tool": "refund_order"}},
                {
                    "template": "arg_equals_denied",
                    "params": {"tool": "charge", "arg": "currency", "value": "XXX"},
                },
            ],
        ).to_dict(),
        "scenario": [
            {"tool": "charge", "args": {"amount": 600, "currency": "USD"}},
            {"tool": "charge", "args": {"amount": 600, "currency": "USD"}},  # cap
            {"tool": "send", "args": {}},
            {"tool": "send", "args": {}},  # count cap
            {"tool": "refund_order", "args": {"order_id": 1}},  # absent → deny
            {"tool": "drop_table", "args": {}},  # deny-list
            {"tool": "webhook", "args": {}, "reversible": False},  # gate
        ],
        "world": [],
    },
]


def _python_comparable(case):
    result = preview(case["spec"], case["scenario"], world=case["world"])
    return {
        "rows": [[r.index, r.tool, r.disposition] for r in result.rows],
        "verdicts": [
            [v.effect_index, v.allow, v.rule_name] for v in result.verdicts
        ],
        "is_clean": result.is_clean,
    }


def test_js_mirror_matches_python_engine(tmp_path):
    assert SITE.exists(), f"missing {SITE}"
    assert RUNNER.exists(), f"missing {RUNNER}"

    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        json.dumps(
            [
                {"spec": c["spec"], "scenario": c["scenario"], "world": c["world"]}
                for c in CASES
            ]
        )
    )

    proc = subprocess.run(
        [NODE, str(RUNNER), str(SITE), str(cases_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    js_results = json.loads(proc.stdout)

    assert len(js_results) == len(CASES)
    for case, js in zip(CASES, js_results):
        py = _python_comparable(case)
        assert js["rows"] == py["rows"], f"rows differ for {case['name']!r}"
        assert js["verdicts"] == py["verdicts"], (
            f"verdicts differ for {case['name']!r}"
        )
        assert js["is_clean"] == py["is_clean"], (
            f"is_clean differs for {case['name']!r}"
        )
