"""Control-plane v2 tests — governed memory, cross-host arbiter, metering.

Skips cleanly without the control-plane extra (FastAPI + httpx), matching
test_control_plane.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="control-plane extra not installed")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.backend.app import create_app  # noqa: E402
from dashboard.backend.db import Store  # noqa: E402

ADMIN = "admin-secret-for-tests"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Store(":memory:"), ADMIN))


def _make_org(client: TestClient, name: str = "Acme") -> tuple[str, dict]:
    r = client.post(
        "/api/v1/orgs", json={"name": name},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["org_id"], {"Authorization": f"Bearer {body['api_key']}"}


# === governed memory over the wire =========================================


def test_memory_remember_recall_forget(client: TestClient) -> None:
    _, auth = _make_org(client)

    r = client.post("/api/v1/memory/ns1/remember", json={"key": "k", "value": "v1"}, headers=auth)
    assert r.status_code == 200
    v1 = r.json()["version"]

    r = client.get("/api/v1/memory/ns1/recall", params={"key": "k"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["value"] == "v1"
    assert r.json()["version"] == v1

    # Overwrite changes the content-addressed version.
    r = client.post("/api/v1/memory/ns1/remember", json={"key": "k", "value": "v2"}, headers=auth)
    assert r.json()["version"] != v1

    r = client.post("/api/v1/memory/ns1/forget", json={"key": "k", "value": ""}, headers=auth)
    assert r.json()["forgotten"] is True

    assert client.get("/api/v1/memory/ns1/recall", params={"key": "k"}, headers=auth).status_code == 404


def test_memory_recall_missing_is_404(client: TestClient) -> None:
    _, auth = _make_org(client)
    assert client.get("/api/v1/memory/ns/recall", params={"key": "nope"}, headers=auth).status_code == 404


def test_memory_list_is_namespaced(client: TestClient) -> None:
    _, auth = _make_org(client)
    client.post("/api/v1/memory/a/remember", json={"key": "x", "value": "1"}, headers=auth)
    client.post("/api/v1/memory/a/remember", json={"key": "y", "value": "2"}, headers=auth)
    client.post("/api/v1/memory/b/remember", json={"key": "z", "value": "3"}, headers=auth)
    keys = {row["key"] for row in client.get("/api/v1/memory/a", headers=auth).json()}
    assert keys == {"x", "y"}


def test_memory_tenant_isolation(client: TestClient) -> None:
    _, auth_a = _make_org(client, "A")
    _, auth_b = _make_org(client, "B")
    client.post("/api/v1/memory/ns/remember", json={"key": "shared", "value": "a-secret"}, headers=auth_a)
    # B uses the same namespace+key name but sees nothing of A's.
    assert client.get("/api/v1/memory/ns/recall", params={"key": "shared"}, headers=auth_b).status_code == 404


# === cross-host arbiter: locks =============================================


def test_lock_acquire_then_conflict_then_release(client: TestClient) -> None:
    _, auth = _make_org(client)

    # Host 1 (holder txn-A) grabs sql keys k1, k2.
    r = client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k1", "k2"], "holder": "txn-A", "ttl": 60},
        headers=auth,
    )
    assert r.json() == {"acquired": True, "conflicts": []}

    # Host 2 (txn-B) wants k2, k3 — k2 conflicts, so nothing is granted.
    r = client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k2", "k3"], "holder": "txn-B", "ttl": 60},
        headers=auth,
    )
    body = r.json()
    assert body["acquired"] is False
    assert body["conflicts"] == [{"key": "k2", "holder": "txn-A"}]

    # txn-A releases; txn-B can now take k2, k3.
    assert client.delete("/api/v1/arbiter/locks/txn-A", headers=auth).json()["released"] == 2
    r = client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k2", "k3"], "holder": "txn-B", "ttl": 60},
        headers=auth,
    )
    assert r.json()["acquired"] is True


def test_lock_reacquire_by_same_holder_is_idempotent(client: TestClient) -> None:
    _, auth = _make_org(client)
    body = {"resource": "sql", "keys": ["k"], "holder": "txn-A", "ttl": 60}
    assert client.post("/api/v1/arbiter/locks", json=body, headers=auth).json()["acquired"] is True
    # Same holder, same key — refresh, not conflict.
    assert client.post("/api/v1/arbiter/locks", json=body, headers=auth).json()["acquired"] is True


def test_expired_lock_is_reclaimable(client: TestClient) -> None:
    _, auth = _make_org(client)
    # ttl=0 → already expired the instant it is written; another holder reclaims.
    client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k"], "holder": "dead", "ttl": 0},
        headers=auth,
    )
    r = client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k"], "holder": "alive", "ttl": 60},
        headers=auth,
    )
    assert r.json()["acquired"] is True


def test_lock_tenant_isolation(client: TestClient) -> None:
    _, auth_a = _make_org(client, "A")
    _, auth_b = _make_org(client, "B")
    client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k"], "holder": "txn-A", "ttl": 60},
        headers=auth_a,
    )
    # B's lock on the same resource/key is independent — no conflict across orgs.
    r = client.post(
        "/api/v1/arbiter/locks",
        json={"resource": "sql", "keys": ["k"], "holder": "txn-B", "ttl": 60},
        headers=auth_b,
    )
    assert r.json()["acquired"] is True


# === cross-host arbiter: budgets ===========================================


def test_budget_spend_and_hard_cap(client: TestClient) -> None:
    _, auth = _make_org(client)

    r = client.post("/api/v1/arbiter/budget/spend-usd/spend", json={"amount": 60, "cap": 100}, headers=auth)
    assert r.json() == {"allowed": True, "spent": 60.0, "cap": 100.0, "remaining": 40.0}

    # A second host spends against the same central counter.
    r = client.post("/api/v1/arbiter/budget/spend-usd/spend", json={"amount": 30}, headers=auth)
    assert r.json()["spent"] == 90.0

    # The one that would cross the cap is denied; spent is unchanged.
    r = client.post("/api/v1/arbiter/budget/spend-usd/spend", json={"amount": 20}, headers=auth)
    body = r.json()
    assert body["allowed"] is False
    assert body["spent"] == 90.0
    assert body["remaining"] == 10.0


def test_budget_first_use_requires_cap(client: TestClient) -> None:
    _, auth = _make_org(client)
    r = client.post("/api/v1/arbiter/budget/new-key/spend", json={"amount": 5}, headers=auth)
    assert r.status_code == 400


def test_budget_tenant_isolation(client: TestClient) -> None:
    _, auth_a = _make_org(client, "A")
    _, auth_b = _make_org(client, "B")
    client.post("/api/v1/arbiter/budget/b/spend", json={"amount": 50, "cap": 50}, headers=auth_a)
    # B's budget under the same key is its own — full headroom.
    r = client.post("/api/v1/arbiter/budget/b/spend", json={"amount": 50, "cap": 50}, headers=auth_b)
    assert r.json()["allowed"] is True


# === minimal metering ======================================================


def test_metering_records_ingest_usage(client: TestClient) -> None:
    org_id, auth = _make_org(client)
    agent = client.post(
        "/api/v1/agents", json={"name": "a", "owner": "u"}, headers=auth
    ).json()

    # Empty before any ingest.
    assert client.get("/api/v1/metering/usage", headers=auth).json()["usage"] == {}

    batch = {
        "agent_id": agent["agent_id"],
        "transactions": [{"txn_id": "t1", "state": "COMMITTED"}],
        "effects": [
            {"txn_id": "t1", "idx": 0, "effect_id": "e0", "tool": "x",
             "resource": "sql", "status": "APPLIED"}
        ],
    }
    client.post("/api/v1/ingest", json=batch, headers=auth)

    usage = client.get("/api/v1/metering/usage", headers=auth).json()["usage"]
    assert usage["transactions_ingested"] == 1
    assert usage["effects_ingested"] == 1


def test_metering_tenant_isolation(client: TestClient) -> None:
    org_a, auth_a = _make_org(client, "A")
    _, auth_b = _make_org(client, "B")
    agent = client.post("/api/v1/agents", json={"name": "a", "owner": "u"}, headers=auth_a).json()
    client.post(
        "/api/v1/ingest",
        json={"agent_id": agent["agent_id"], "transactions": [{"txn_id": "t", "state": "OPEN"}]},
        headers=auth_a,
    )
    # B sees none of A's usage.
    assert client.get("/api/v1/metering/usage", headers=auth_b).json()["usage"] == {}


# === unauthenticated access is rejected on every v2 surface ================


@pytest.mark.parametrize(
    "method,path,json",
    [
        ("post", "/api/v1/memory/ns/remember", {"key": "k", "value": "v"}),
        ("get", "/api/v1/memory/ns/recall?key=k", None),
        ("post", "/api/v1/arbiter/locks", {"resource": "sql", "keys": ["k"], "holder": "h"}),
        ("post", "/api/v1/arbiter/budget/b/spend", {"amount": 1, "cap": 1}),
        ("get", "/api/v1/metering/usage", None),
    ],
)
def test_v2_surfaces_require_org_key(client: TestClient, method, path, json) -> None:
    resp = getattr(client, method)(path, json=json) if json is not None else getattr(client, method)(path)
    assert resp.status_code == 401
