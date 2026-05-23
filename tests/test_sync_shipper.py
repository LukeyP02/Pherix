"""Tests for JournalShipper — cursor-based, encrypted, non-blocking ship.

Crypto needs ``cryptography`` (the ``pherix[sync]`` extra). The end-to-end test
also needs the control-plane extra (FastAPI + httpx for TestClient).
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("cryptography")

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect
from pherix.core.transaction import Transaction
from pherix.sync.crypto import PayloadCipher, generate_key
from pherix.sync.shipper import JournalShipper


# --- helpers ----------------------------------------------------------------


def _record_txn(journal, *, args=None, result=None):
    """Record one txn + one effect (with args/result) + one verdict; return txn_id."""
    txn = Transaction()
    journal.record_transaction(txn)
    e = Effect(
        txn_id=txn.txn_id,
        index=0,
        tool="charge_card",
        args=args if args is not None else {"amount": 100},
        resource="http",
        reversible=False,
    )
    if result is not None:
        e.result = result
    journal.record_effect(e)
    journal.record_verdicts(
        txn.txn_id,
        [{"effect_index": 0, "phase": "commit", "allow": True, "kind": "rule"}],
    )
    return txn.txn_id


class _Recorder:
    """A transport that records every batch and returns a plausible ingest result."""

    def __init__(self, fail_times: int = 0):
        self.batches: list[dict] = []
        self._fail_times = fail_times

    def __call__(self, batch: dict) -> dict:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("transport down")
        self.batches.append(batch)
        n = len(batch["transactions"]) + len(batch["effects"]) + len(batch["verdicts"])
        return {"accepted": n, "skipped": 0, "cursor": n}


# --- cursor + drain ---------------------------------------------------------


def test_pump_ships_new_rows_then_nothing():
    j = AuditJournal.in_memory()
    _record_txn(j)
    rec = _Recorder()
    shipper = JournalShipper(j, "agent-1", rec, key=generate_key())

    r1 = shipper.pump()
    assert r1["shipped"] == 3  # 1 txn + 1 effect + 1 verdict
    assert len(rec.batches) == 1
    assert rec.batches[0]["agent_id"] == "agent-1"

    # Cursor advanced — a second pump with no new rows ships nothing.
    r2 = shipper.pump()
    assert r2["shipped"] == 0
    assert len(rec.batches) == 1


def test_only_new_rows_ship_after_cursor_advance():
    j = AuditJournal.in_memory()
    _record_txn(j)
    rec = _Recorder()
    shipper = JournalShipper(j, "a", rec, key=generate_key())
    shipper.pump()

    _record_txn(j)  # a second transaction after the first ship
    r = shipper.pump()
    assert r["shipped"] == 3
    assert len(rec.batches) == 2


# --- encryption boundary ----------------------------------------------------


def test_payload_is_encrypted_and_round_trips():
    j = AuditJournal.in_memory()
    _record_txn(j, args={"amount": 4200}, result={"ok": True})
    key = generate_key()
    rec = _Recorder()
    JournalShipper(j, "a", rec, key=key).pump()

    eff = rec.batches[0]["effects"][0]
    assert rec.batches[0]["encrypted"] is True
    # The shipped args are ciphertext — not the cleartext JSON.
    assert "4200" not in (eff["args"] or "")
    # The customer (holding the key) recovers the original payload.
    cipher = PayloadCipher(key)
    assert json.loads(cipher.decrypt(eff["args"])) == {"amount": 4200}
    assert json.loads(cipher.decrypt(eff["result"])) == {"ok": True}


def test_cleartext_mode_when_no_key():
    j = AuditJournal.in_memory()
    _record_txn(j, args={"amount": 7})
    rec = _Recorder()
    JournalShipper(j, "a", rec).pump()  # no key
    eff = rec.batches[0]["effects"][0]
    assert rec.batches[0]["encrypted"] is False
    assert json.loads(eff["args"]) == {"amount": 7}


def test_pii_redacted_before_encryption():
    j = AuditJournal.in_memory()
    _record_txn(j, args={"email": "a@b.com", "amount": 9})
    key = generate_key()
    rec = _Recorder()
    JournalShipper(j, "a", rec, key=key).pump()
    eff = rec.batches[0]["effects"][0]
    recovered = json.loads(PayloadCipher(key).decrypt(eff["args"]))
    assert recovered["email"] == "«redacted»"
    assert recovered["amount"] == 9


# --- failure / idempotency --------------------------------------------------


def test_transport_failure_leaves_cursor_unadvanced():
    j = AuditJournal.in_memory()
    _record_txn(j)
    rec = _Recorder(fail_times=1)
    shipper = JournalShipper(j, "a", rec, key=generate_key())

    with pytest.raises(RuntimeError, match="transport down"):
        shipper.pump()
    # Cursor not advanced — the same rows re-ship on the next (successful) pump.
    r = shipper.pump()
    assert r["shipped"] == 3
    assert len(rec.batches) == 1


def test_metadata_is_cleartext_even_when_encrypted():
    # The shape moat: tool/resource/status stay readable; only the payload is opaque.
    j = AuditJournal.in_memory()
    _record_txn(j)
    rec = _Recorder()
    JournalShipper(j, "a", rec, key=generate_key()).pump()
    eff = rec.batches[0]["effects"][0]
    assert eff["tool"] == "charge_card"
    assert eff["resource"] == "http"
    assert eff["reversible"] is False


# --- background operation ---------------------------------------------------


def test_start_rejects_in_memory_journal():
    shipper = JournalShipper(AuditJournal.in_memory(), "a", _Recorder())
    with pytest.raises(ValueError, match="in-memory"):
        shipper.start()


def test_background_thread_ships_without_blocking(tmp_path):
    path = str(tmp_path / "journal.db")
    agent_journal = AuditJournal(path)  # the agent's own connection
    rec = _Recorder()
    shipper = JournalShipper(agent_journal, "a", rec, key=generate_key())

    shipper.start(interval=0.02)
    try:
        _record_txn(agent_journal)  # agent keeps writing on its thread
        # Poll for the background thread (its own connection) to pick the rows up.
        deadline = time.time() + 3.0
        while not rec.batches and time.time() < deadline:
            time.sleep(0.02)
        assert rec.batches, "background shipper did not ship within the deadline"
    finally:
        shipper.stop()


# --- end-to-end against the control plane -----------------------------------


def test_end_to_end_control_plane_stores_ciphertext_it_cannot_read():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from dashboard.backend.app import create_app
    from dashboard.backend.db import Store

    store = Store(":memory:")
    client = TestClient(create_app(store, "admin-secret"))

    # Stand up an org + agent.
    r = client.post(
        "/api/v1/orgs", json={"name": "Acme"},
        headers={"Authorization": "Bearer admin-secret"},
    )
    org = r.json()
    org_auth = {"Authorization": f"Bearer {org['api_key']}"}
    org_id = org["org_id"]
    agent = client.post(
        "/api/v1/agents", json={"name": "fleet-1", "owner": "u@acme"}, headers=org_auth
    ).json()

    # Ship an encrypted journal through the real ingest endpoint.
    j = AuditJournal.in_memory()
    _record_txn(j, args={"amount": 4200, "email": "a@b.com"})
    key = generate_key()

    def transport(batch: dict) -> dict:
        resp = client.post("/api/v1/ingest", json=batch, headers=org_auth)
        resp.raise_for_status()
        return resp.json()

    shipper = JournalShipper(j, agent["agent_id"], transport, key=key)
    result = shipper.pump()
    assert result["shipped"] == 3  # txn + effect + verdict

    # The control plane returns ciphertext — it cannot read "4200" or the email.
    effects = store.search_effects(org_id, tool="charge_card")
    assert len(effects) == 1
    stored_args = effects[0]["args"]
    assert "4200" not in stored_args
    assert "a@b.com" not in stored_args

    # The control plane recorded that this payload is opaque to it (enc = 1).
    with store._tx() as conn:  # noqa: SLF001 — test reaches into the store to assert the column
        enc = conn.execute(
            "SELECT enc FROM ingest_effects WHERE org_id = ? AND tool = 'charge_card'",
            (org_id,),
        ).fetchone()["enc"]
    assert enc == 1

    # Only the customer, holding the key, recovers the payload (email redacted).
    recovered = json.loads(PayloadCipher(key).decrypt(stored_args))
    assert recovered["amount"] == 4200
    assert recovered["email"] == "«redacted»"
