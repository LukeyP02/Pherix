"""The site API must be the engine, not a re-implementation of it.

``server/app.py`` exists so the governance page's preview runs the genuine
``pherix.governance.preview`` over HTTP. These tests pin that: the JSON the
endpoint returns is byte-for-byte the projection of the in-process engine result.
If the endpoint ever drifts from the engine, this fails — the same guarantee the
JS conformance test gives the browser mirror.

The whole module skips cleanly when the `site` extra isn't installed (FastAPI +
httpx for the test client), matching the adapter suites' optional-driver pattern.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="site extra not installed (pip install .[site])")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from pherix.governance import STARTER_TEMPLATES, preview, to_python  # noqa: E402
from server.app import app  # noqa: E402

client = TestClient(app)


SPEC = {
    "name": "spend-capped",
    "description": "",
    "allow": None,
    "deny": [],
    "caps": [
        {"kind": "sum", "tool": "charge", "max": 1000, "field": "amount"},
        {"kind": "count", "tool": "send_email", "max": 5, "field": None},
    ],
    "rules": [],
    "gate_irreversibles": True,
}
SCENARIO = [
    {"tool": "charge", "args": {"amount": 600}, "reversible": True, "compensator": "refund"},
    {"tool": "charge", "args": {"amount": 600}, "reversible": True, "compensator": "refund"},
    {"tool": "send_email", "args": {}, "reversible": False, "compensator": None},
]


def _engine_projection(spec, scenario, world):
    """The exact dict the endpoint promises to return, computed in-process."""
    result = preview(spec, scenario, world=world)
    return {
        "counts": result.counts,
        "is_clean": result.is_clean,
        "rows": [
            {"index": r.index, "tool": r.tool, "disposition": r.disposition, "reasons": r.reasons}
            for r in result.rows
        ],
    }


def test_preview_endpoint_is_the_engine():
    """The endpoint's JSON equals the in-process engine's result — no drift."""
    resp = client.post("/api/preview", json={"spec": SPEC, "scenario": SCENARIO, "world": []})
    assert resp.status_code == 200
    assert resp.json() == _engine_projection(SPEC, SCENARIO, [])


def test_preview_with_world_state_rule():
    """A world-state rule (refund_if_paid) folds identically through the API."""
    spec = {
        "name": "refund-guarded",
        "description": "",
        "allow": None,
        "deny": [],
        "caps": [],
        "rules": [
            {
                "template": "refund_if_paid",
                "params": {
                    "tool": "refund_order",
                    "table": "orders",
                    "id_arg": "order_id",
                    "pk_column": "id",
                    "status_column": "status",
                    "paid_value": "paid",
                    "resource": "sql",
                },
            }
        ],
        "gate_irreversibles": True,
    }
    scenario = [
        {"tool": "refund_order", "args": {"order_id": 42}, "reversible": False, "compensator": None},
    ]
    world = [{"resource": "sql", "key": ["orders", "id", 42, "status"], "value": "paid"}]
    resp = client.post("/api/preview", json={"spec": spec, "scenario": scenario, "world": world})
    assert resp.status_code == 200
    assert resp.json() == _engine_projection(spec, scenario, world)


def test_bad_spec_returns_400_not_500():
    """An unknown rule template is a 400 (the UI falls back), never a crash."""
    spec = {**SPEC, "rules": [{"template": "no_such_template", "params": {}}]}
    resp = client.post("/api/preview", json={"spec": spec, "scenario": SCENARIO})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_templates_endpoint_serves_the_catalog():
    resp = client.get("/api/templates")
    assert resp.status_code == 200
    assert resp.json() == [t.to_dict() for t in STARTER_TEMPLATES]


def test_policy_python_export_is_engine_codegen():
    resp = client.post("/api/policy/python", json={"spec": SPEC})
    assert resp.status_code == 200
    assert resp.json() == {"code": to_python(SPEC)}


def test_health_and_static_root():
    assert client.get("/api/health").json()["status"] == "ok"
    root = client.get("/")
    assert root.status_code == 200
    assert "<!doctype html>" in root.text.lower()
