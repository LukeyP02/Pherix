"""Control-plane service tests — multi-tenant identity, fleet, policy
distribution, journal ingest, and cross-host audit search.

The whole module skips cleanly when the control-plane extra isn't installed
(FastAPI + httpx for the test client), matching the adapter suites' and
``test_site_api`` optional-dependency pattern. The OSS SDK never imports any of
this and the default suite stays fully offline.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="control-plane extra not installed (pip install .[control-plane])")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.backend.app import create_app  # noqa: E402
from dashboard.backend.db import Store  # noqa: E402

ADMIN = "admin-secret-for-tests"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Store(":memory:"), ADMIN))


def _admin(h: str = ADMIN) -> dict:
    return {"Authorization": f"Bearer {h}"}


def _make_org(client: TestClient, name: str = "Acme") -> tuple[str, dict]:
    """Create an org as admin; return (org_id, auth-header for that org)."""
    r = client.post("/api/v1/orgs", json={"name": name}, headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    return body["org_id"], {"Authorization": f"Bearer {body['api_key']}"}


# -- health + auth -----------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["service"] == "pherix-control-plane"


def test_org_creation_requires_admin(client: TestClient) -> None:
    assert client.post("/api/v1/orgs", json={"name": "X"}).status_code == 401
    assert (
        client.post("/api/v1/orgs", json={"name": "X"}, headers=_admin("wrong")).status_code
        == 401
    )
    assert client.post("/api/v1/orgs", json={"name": "X"}, headers=_admin()).status_code == 200


def test_org_endpoints_require_valid_key(client: TestClient) -> None:
    _, org = _make_org(client)
    assert client.get("/api/v1/agents").status_code == 401  # no key
    assert client.get("/api/v1/agents", headers={"Authorization": "Bearer pk_bogus"}).status_code == 401
    assert client.get("/api/v1/agents", headers=org).status_code == 200


def test_key_mint_and_revoke(client: TestClient) -> None:
    _, org = _make_org(client)
    r = client.post("/api/v1/keys", json={"name": "ci"}, headers=org)
    assert r.status_code == 200
    minted = r.json()["api_key"]
    ci = {"Authorization": f"Bearer {minted}"}
    assert client.get("/api/v1/agents", headers=ci).status_code == 200  # works

    # find the key_id and revoke it
    key_id = next(k["key_id"] for k in client.get("/api/v1/keys", headers=org).json() if k["name"] == "ci")
    assert client.delete(f"/api/v1/keys/{key_id}", headers=org).status_code == 200
    assert client.get("/api/v1/agents", headers=ci).status_code == 401  # revoked → rejected


def test_plaintext_key_never_persisted(client: TestClient) -> None:
    _, org = _make_org(client)
    # listing keys returns metadata only — no plaintext field anywhere
    for k in client.get("/api/v1/keys", headers=org).json():
        assert "api_key" not in k and "key_hash" not in k


# -- tenant isolation --------------------------------------------------------


def test_tenant_isolation(client: TestClient) -> None:
    _, org_a = _make_org(client, "A")
    _, org_b = _make_org(client, "B")
    client.post("/api/v1/agents", json={"name": "a-bot", "owner": "a@x"}, headers=org_a)
    # org B sees none of org A's agents
    assert client.get("/api/v1/agents", headers=org_b).json() == []
    a_agents = client.get("/api/v1/agents", headers=org_a).json()
    assert len(a_agents) == 1
    # org B cannot fetch org A's agent by id
    assert client.get(f"/api/v1/agents/{a_agents[0]['agent_id']}", headers=org_b).status_code == 404


# -- users + agents ----------------------------------------------------------


def test_users_are_opaque_references(client: TestClient) -> None:
    _, org = _make_org(client)
    r = client.post("/api/v1/users", json={"ref": "alice@corp.com", "role": "approver"}, headers=org)
    assert r.status_code == 200
    assert r.json()["ref"] == "alice@corp.com"
    assert any(u["ref"] == "alice@corp.com" for u in client.get("/api/v1/users", headers=org).json())


def test_agent_registry(client: TestClient) -> None:
    _, org = _make_org(client)
    r = client.post("/api/v1/agents", json={"name": "support-bot", "owner": "team-x"}, headers=org)
    agent_id = r.json()["agent_id"]
    assert r.json()["owner"] == "team-x"
    assert client.get(f"/api/v1/agents/{agent_id}", headers=org).json()["name"] == "support-bot"
    assert client.get("/api/v1/agents/agt_missing", headers=org).status_code == 404


# -- policy distribution -----------------------------------------------------


def _make_agent(client: TestClient, org: dict) -> str:
    return client.post(
        "/api/v1/agents", json={"name": "bot", "owner": "o"}, headers=org
    ).json()["agent_id"]


def _make_policy_v1(client: TestClient, org: dict, spec: dict) -> str:
    pid = client.post("/api/v1/policies", json={"name": "spend"}, headers=org).json()["policy_id"]
    client.post(f"/api/v1/policies/{pid}/versions", json={"spec": spec}, headers=org)
    return pid


def test_policy_versions_increment(client: TestClient) -> None:
    _, org = _make_org(client)
    pid = client.post("/api/v1/policies", json={"name": "spend"}, headers=org).json()["policy_id"]
    v1 = client.post(f"/api/v1/policies/{pid}/versions", json={"spec": {"caps": [1]}}, headers=org)
    v2 = client.post(f"/api/v1/policies/{pid}/versions", json={"spec": {"caps": [2]}}, headers=org)
    assert v1.json()["version"] == 1
    assert v2.json()["version"] == 2
    assert [v["version"] for v in client.get(f"/api/v1/policies/{pid}/versions", headers=org).json()] == [1, 2]


def test_pull_pinned_version(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    pid = _make_policy_v1(client, org, {"caps": ["v1"]})
    client.post(f"/api/v1/policies/{pid}/versions", json={"spec": {"caps": ["v2"]}}, headers=org)
    # pin to version 1
    client.put(f"/api/v1/agents/{agent_id}/policy", json={"policy_id": pid, "version": 1}, headers=org)
    pulled = client.get(f"/api/v1/agents/{agent_id}/policy", headers=org).json()
    assert pulled["version"] == 1
    assert pulled["spec"] == {"caps": ["v1"]}


def test_pull_latest_follows_new_versions(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    pid = _make_policy_v1(client, org, {"caps": ["v1"]})
    # assign 'latest' (null version)
    client.put(f"/api/v1/agents/{agent_id}/policy", json={"policy_id": pid, "version": None}, headers=org)
    assert client.get(f"/api/v1/agents/{agent_id}/policy", headers=org).json()["version"] == 1
    # push a new version; the agent on 'latest' now pulls it
    client.post(f"/api/v1/policies/{pid}/versions", json={"spec": {"caps": ["v2"]}}, headers=org)
    pulled = client.get(f"/api/v1/agents/{agent_id}/policy", headers=org).json()
    assert pulled["version"] == 2 and pulled["spec"] == {"caps": ["v2"]}


def test_assign_unknown_policy_version_404(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    pid = _make_policy_v1(client, org, {"caps": []})
    r = client.put(f"/api/v1/agents/{agent_id}/policy", json={"policy_id": pid, "version": 99}, headers=org)
    assert r.status_code == 404


def test_pull_without_assignment_404(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    assert client.get(f"/api/v1/agents/{agent_id}/policy", headers=org).status_code == 404


# -- journal ingest ----------------------------------------------------------


def _batch(agent_id: str, txn_id: str, session_id: str = "sess-1", host: str = "host-a") -> dict:
    return {
        "agent_id": agent_id,
        "host": host,
        "transactions": [
            {"txn_id": txn_id, "state": "COMMITTED", "session_id": session_id, "ts": None}
        ],
        "effects": [
            {"txn_id": txn_id, "idx": 0, "effect_id": f"{txn_id}-0", "tool": "charge",
             "resource": "stripe", "reversible": True, "status": "APPLIED",
             "args": '{"amount": 600}', "ts": "2026-05-20T10:00:00+00:00"},
        ],
        "verdicts": [
            {"txn_id": txn_id, "effect_index": 0, "seq_in_txn": 0, "phase": "commit",
             "allow": True, "kind": "cap", "rule_name": "spend-cap"},
        ],
    }


def test_ingest_unknown_agent_404(client: TestClient) -> None:
    _, org = _make_org(client)
    assert client.post("/api/v1/ingest", json=_batch("agt_nope", "t1"), headers=org).status_code == 404


def test_ingest_and_cursor(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    r = client.post("/api/v1/ingest", json=_batch(agent_id, "t1"), headers=org)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 3 and body["skipped"] == 0
    assert client.get("/api/v1/ingest/cursor", headers=org).json()["cursor"] == body["cursor"] == 3


def test_ingest_is_idempotent(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    client.post("/api/v1/ingest", json=_batch(agent_id, "t1"), headers=org)
    # re-ship the identical batch — every row already exists
    second = client.post("/api/v1/ingest", json=_batch(agent_id, "t1"), headers=org).json()
    assert second["accepted"] == 0 and second["skipped"] == 3
    # exactly one effect remains, not two
    assert client.get("/api/v1/audit/effects", headers=org).json()["count"] == 1


# -- cross-host audit search -------------------------------------------------


def test_search_effects_filters_and_isolation(client: TestClient) -> None:
    _, org_a = _make_org(client, "A")
    _, org_b = _make_org(client, "B")
    a_agent = _make_agent(client, org_a)
    b_agent = _make_agent(client, org_b)
    client.post("/api/v1/ingest", json=_batch(a_agent, "ta", session_id="s-a"), headers=org_a)
    client.post("/api/v1/ingest", json=_batch(b_agent, "tb", session_id="s-b"), headers=org_b)
    # org A only sees its own effect
    res = client.get("/api/v1/audit/effects", headers=org_a).json()
    assert res["count"] == 1 and res["results"][0]["txn_id"] == "ta"
    # tool filter
    assert client.get("/api/v1/audit/effects?tool=charge", headers=org_a).json()["count"] == 1
    assert client.get("/api/v1/audit/effects?tool=refund", headers=org_a).json()["count"] == 0
    # session join carried from the parent transaction
    assert res["results"][0]["session_id"] == "s-a"
    assert res["results"][0]["host"] == "host-a"


def test_search_verdicts_denied(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    batch = _batch(agent_id, "t1")
    batch["verdicts"].append(
        {"txn_id": "t1", "effect_index": 1, "seq_in_txn": 1, "phase": "stage",
         "allow": False, "kind": "rule", "rule_name": "deny-payouts", "reason": "blocked"}
    )
    client.post("/api/v1/ingest", json=batch, headers=org)
    denied = client.get("/api/v1/audit/verdicts?allow=false", headers=org).json()
    assert denied["count"] == 1
    assert denied["results"][0]["rule_name"] == "deny-payouts"


def test_session_timeline_groups_and_404(client: TestClient) -> None:
    _, org = _make_org(client)
    agent_id = _make_agent(client, org)
    client.post("/api/v1/ingest", json=_batch(agent_id, "t1", session_id="sess-X"), headers=org)
    client.post("/api/v1/ingest", json=_batch(agent_id, "t2", session_id="sess-X"), headers=org)
    timeline = client.get("/api/v1/audit/sessions/sess-X", headers=org).json()
    assert {t["txn_id"] for t in timeline["transactions"]} == {"t1", "t2"}
    assert len(timeline["effects"]) == 2
    assert client.get("/api/v1/audit/sessions/nope", headers=org).status_code == 404
