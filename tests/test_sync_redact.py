"""Unit tests for edge redaction — PII dropped before it leaves the host."""

from __future__ import annotations

import json

from pherix.sync.redact import REDACTED, Redactor


def test_pii_named_keys_are_masked():
    r = Redactor()
    out = json.loads(r.redact_json(json.dumps({"email": "a@b.com", "amount": 42})))
    assert out["email"] == REDACTED
    assert out["amount"] == 42


def test_recurses_into_nested_structures():
    r = Redactor()
    payload = json.dumps(
        {"user": {"ssn": "123-45-6789", "name": "ok"}, "items": [{"token": "xyz"}]}
    )
    out = json.loads(r.redact_json(payload))
    assert out["user"]["ssn"] == REDACTED
    assert out["user"]["name"] == "ok"
    assert out["items"][0]["token"] == REDACTED


def test_case_insensitive_key_match():
    r = Redactor()
    out = json.loads(r.redact_json(json.dumps({"API_Key": "k", "Email": "e"})))
    assert out["API_Key"] == REDACTED
    assert out["Email"] == REDACTED


def test_none_and_non_json_pass_through():
    r = Redactor()
    assert r.redact_json(None) is None
    assert r.redact_json("not json at all") == "not json at all"


def test_custom_key_set():
    r = Redactor(keys={"customer_ref"})
    out = json.loads(r.redact_json(json.dumps({"customer_ref": "C9", "email": "e"})))
    assert out["customer_ref"] == REDACTED
    # Defaults are replaced, not merged — "email" is no longer redacted.
    assert out["email"] == "e"


def test_output_is_canonical():
    # Re-shipping the same payload must be byte-equal (sorted keys) so idempotent
    # ingest sees an identical row.
    r = Redactor()
    a = r.redact_json(json.dumps({"b": 1, "a": 2}))
    b = r.redact_json(json.dumps({"a": 2, "b": 1}))
    assert a == b
