"""Deterministic, offline tests for the watchable demo session JSON.

The session is driven through the real Pherix engine (no LLM, no network, no
key). These tests pin the contract the web player relies on: a time-ordered
timeline, exactly one gate on the irreversible payment, an untouched world, a
real journal read back from the AuditJournal, and byte-identical output across
runs.
"""

from __future__ import annotations

import json

from examples.demo import session

# The real EffectStatus members — the journal stores the enum NAME (uppercase),
# the animated timeline uses the enum .value (lowercase). Either casing is a
# real engine status; free text would fail this set.
REAL_STATUSES = {"STAGED", "APPLIED", "COMPENSATED", "GATED", "FAILED"}


def test_timeline_is_monotonic_in_t() -> None:
    s = session.build_session()
    ts = [event["t"] for event in s["timeline"]]
    assert ts == sorted(ts), "timeline must be strictly ordered by t"
    assert all(isinstance(t, float) for t in ts)


def test_exactly_one_gate_on_send_payment() -> None:
    s = session.build_session()
    gates = [e for e in s["timeline"] if e["kind"] == "gate"]
    assert len(gates) == 1
    gate = gates[0]
    assert gate["status"] == "GATED"
    # the gated effect must be the send_payment tool_call's effect_id
    pay_calls = [
        e
        for e in s["timeline"]
        if e["kind"] == "tool_call" and e["tool"] == "send_payment"
    ]
    assert len(pay_calls) == 1
    assert gate["effect_id"] == pay_calls[0]["effect_id"]
    assert pay_calls[0]["reversible"] is False
    assert gate["reason"]


def test_world_untouched_and_checks_pass() -> None:
    s = session.build_session()
    assert s["verdict"]["world"]["egress_charges"] == 0
    # db rows restored to the seed count (5 in acts.SEED_CUSTOMERS)
    assert s["verdict"]["world"]["db_rows"] == 5
    checks = s["verdict"]["checks"]
    assert checks == {"undo": True, "gate": True, "audit": True}


def test_journal_is_real_and_nonempty() -> None:
    s = session.build_session()
    journal = s["journal"]
    assert journal, "journal must be non-empty"
    for row in journal:
        assert row["status"].upper() in REAL_STATUSES, row
        assert row["tool"]
        assert row["resource"] in {"sql", "http"}
        assert row["effect_id"]
    tools = {row["tool"] for row in journal}
    assert "purge_churned_accounts" in tools
    assert "send_payment" in tools


def test_wrap_uses_real_api() -> None:
    s = session.build_session()
    after = "\n".join(s["wrap"]["after"])
    # the after block must show the genuine Pherix wrap surface
    assert "@tool" in after
    assert "agent_txn" in after
    assert "SQLiteAdapter" in after and "HTTPAdapter" in after


def test_output_is_byte_identical_across_runs() -> None:
    a = json.dumps(session.build_session(), indent=2, sort_keys=True) + "\n"
    b = json.dumps(session.build_session(), indent=2, sort_keys=True) + "\n"
    assert a == b


def test_write_produces_deterministic_file(tmp_path) -> None:
    p1 = tmp_path / "one.json"
    p2 = tmp_path / "two.json"
    session.write(p1)
    session.write(p2)
    assert p1.read_text() == p2.read_text()
