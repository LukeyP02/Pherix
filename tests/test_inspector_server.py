"""The inspector HTTP layer — real requests against a live server.

Spins the stdlib server on an ephemeral port over a seeded journal, then
exercises every route with urllib: the static frontend, the JSON API,
filters as query params, 404s, and that the static allow-list blocks
anything not explicitly served. Offline (localhost only).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pherix.inspector.seed import seed_demo_journal
from pherix.inspector.server import make_server


@pytest.fixture
def server(tmp_path: Path):
    db = str(tmp_path / "demo.db")
    seed_demo_journal(db)
    httpd = make_server(db, host="127.0.0.1", port=0)  # 0 → OS picks a free port
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.reader.close()
        httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def _get_json(url: str):
    status, ctype, body = _get(url)
    assert "application/json" in ctype
    return status, json.loads(body)


def _status_of(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# --- static -----------------------------------------------------------------


def test_root_serves_html(server: str):
    status, ctype, body = _get(server + "/")
    assert status == 200
    assert "text/html" in ctype
    assert b"pherix" in body.lower()


def test_static_assets_served(server: str):
    for name, frag in [("app.js", b"inspector"), ("style.css", b"--bg")]:
        status, _, body = _get(f"{server}/static/{name}")
        assert status == 200
        assert frag in body


def test_static_allowlist_blocks_unknown(server: str):
    assert _status_of(server + "/static/server.py") == 404
    assert _status_of(server + "/static/secrets.txt") == 404


# --- api ---------------------------------------------------------------------


def test_api_stats(server: str):
    status, data = _get_json(server + "/api/stats")
    assert status == 200
    assert data["txn_total"] == 7
    assert data["effect_total"] == 14


def test_api_list_all(server: str):
    status, data = _get_json(server + "/api/transactions")
    assert status == 200
    assert len(data) == 7
    assert {"txn_id", "state", "tone", "effect_count"} <= set(data[0])


def test_api_list_filter_by_state(server: str):
    _, data = _get_json(server + "/api/transactions?state=STUCK")
    assert [t["txn_id"] for t in data] == ["txn-stuck-payout04"]


def test_api_list_filter_by_client(server: str):
    _, data = _get_json(server + "/api/transactions?client_id=claude-code")
    assert [t["txn_id"] for t in data] == ["txn-clientA-q06"]


def test_api_list_hide_dry_run(server: str):
    _, data = _get_json(server + "/api/transactions?include_dry_run=0")
    ids = {t["txn_id"] for t in data}
    assert "txn-dryrun-plan05" not in ids
    assert len(ids) == 6


def test_api_list_limit(server: str):
    _, data = _get_json(server + "/api/transactions?limit=3")
    assert len(data) == 3


def test_api_timeline(server: str):
    status, data = _get_json(server + "/api/transactions/txn-gated-charge03")
    assert status == 200
    assert data["transaction"]["state"] == "STAGED"
    tools = [(e["tool"], e["status"], e["tone"]) for e in data["effects"]]
    assert ("charge_card", "GATED", "blocked") in tools


def test_api_timeline_missing_is_404(server: str):
    assert _status_of(server + "/api/transactions/txn-nope") == 404


def test_api_unknown_route_is_404(server: str):
    assert _status_of(server + "/api/whatever") == 404


# --- reliability (Prong #2) -------------------------------------------------


def test_api_reliability_default_excludes_dry_run(server: str):
    status, data = _get_json(server + "/api/reliability")
    assert status == 200
    assert data["scope"]["include_dry_run"] is False
    # Settled over the 6 non-dry-run txns: COMMITTED 3 + ROLLED_BACK 1 + STUCK 1.
    assert data["outcomes"]["settled"] == 5
    assert data["effects"]["total"] == 12
    assert data["conflict_total"] == 0
    # failed-before-gated ranking surfaces over the wire.
    assert [t["tool"] for t in data["top_failing_tools"]] == [
        "notify_vendor",
        "charge_card",
    ]


def test_api_reliability_include_dry_run_flag(server: str):
    _, excl = _get_json(server + "/api/reliability?include_dry_run=0")
    _, incl = _get_json(server + "/api/reliability?include_dry_run=1")
    assert incl["effects"]["total"] == excl["effects"]["total"] + 2
    assert incl["scope"]["include_dry_run"] is True


# --- lineage (action-provenance) -------------------------------------------


def test_api_lineage_whole_journal(server: str):
    status, data = _get_json(server + "/api/lineage")
    assert status == 200
    assert set(data) >= {"scope", "nodes", "edges", "chains", "caveat"}
    assert data["scope"]["txn_id"] is None
    # the seed's read_release → bump_version 'informs' edge survives the round trip
    informs = [(e["from"], e["to"]) for e in data["edges"] if e["kind"] == "informs"]
    assert ("txn-clean-deploy01#0", "txn-clean-deploy01#1") in informs
    assert "action provenance" in data["caveat"].lower()


def test_api_lineage_txn_scoped(server: str):
    _, data = _get_json(server + "/api/lineage?txn=txn-clean-deploy01")
    assert data["scope"]["txn_id"] == "txn-clean-deploy01"
    assert {c["txn_id"] for c in data["chains"]} == {"txn-clean-deploy01"}
