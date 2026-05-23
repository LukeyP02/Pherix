"""Baseline ("before") mechanism test for the audit dogfood — isolation off vs on.

NOT a real-agent run. Two reconcilers contend on ONE ledger entry; each
independently books the single -50 correction it needs. We assert the contrast
the before/after demo films: with isolation **off** (the before), neither agent
saw the other's write, so both corrections land and the entry over-corrects — the
lost update; with isolation **on** (the after), the second committer's stale read
is aborted, so exactly one correction lands and the entry reaches its expected
value. Deterministic in both worlds (the interleave is explicit, not a thread
race), offline, no key.
"""

from examples.dogfood.audit import (
    AUDIT_TOOLS,
    CONTENDED_ENTRY,
    EXPECTED_AMOUNTS,
    LEDGER_SCHEMA,
    run_contended_reconciliation,
)
from examples.dogfood.capture import audit_before_after
from examples.dogfood.infra import scratch_sqlite

import os
import tempfile

import pytest

from pherix.core.tools import REGISTRY


@pytest.fixture(autouse=True)
def _register_audit_tools():
    # The audit @tools register at import time; the autouse conftest clears the
    # registry around each test, so put their specs back (the governed path's
    # record_tool_call resolves the spec by name in REGISTRY).
    for wrapper in AUDIT_TOOLS:
        if wrapper.tool_spec.name not in REGISTRY:
            REGISTRY.register(wrapper.tool_spec)
    yield


def _audit_path():
    fd, path = tempfile.mkstemp(suffix=".audit.db")
    os.close(fd)
    return path


def test_ungoverned_contention_over_corrects_the_entry():
    """The before world: two reconcilers each book the -50 because neither saw the
    other's write — the entry over-corrects (the lost update)."""
    path = _audit_path()
    try:
        with scratch_sqlite(LEDGER_SCHEMA) as db:
            out = run_contended_reconciliation(
                db=db, audit_path=path, governed=False
            )
        # Both -50 corrections landed → the entry is pushed past expected.
        assert len(out.adjustments) == 2
        assert out.effective_amount == EXPECTED_AMOUNTS[CONTENDED_ENTRY] - 50
        assert out.corrupted is True
        assert out.conflict is False
    finally:
        for sib in (path, path + "-wal", path + "-shm"):
            try:
                os.unlink(sib)
            except FileNotFoundError:
                pass


def test_governed_contention_corrects_once_and_isolates():
    """The after world: the isolation engine aborts the stale reader, so exactly
    one correction lands and the entry reaches its expected value."""
    path = _audit_path()
    try:
        with scratch_sqlite(LEDGER_SCHEMA) as db:
            out = run_contended_reconciliation(
                db=db, audit_path=path, governed=True
            )
        # Exactly one correction survived; the entry is correct, the conflict fired.
        assert len(out.adjustments) == 1
        assert out.effective_amount == EXPECTED_AMOUNTS[CONTENDED_ENTRY]
        assert out.corrupted is False
        assert out.conflict is True
    finally:
        for sib in (path, path + "-wal", path + "-shm"):
            try:
                os.unlink(sib)
            except FileNotFoundError:
                pass


def test_before_after_audit_contrast():
    """Same contended reconciliation, both worlds: before corrupts, after is clean."""
    ba = audit_before_after()

    assert ba.before.harmed is True
    assert ba.before.proof["effective_amount"] == EXPECTED_AMOUNTS[CONTENDED_ENTRY] - 50
    assert len(ba.before.proof["adjustments"]) == 2

    assert ba.after.harmed is False
    assert ba.after.proof["effective_amount"] == EXPECTED_AMOUNTS[CONTENDED_ENTRY]
    assert ba.after.proof["isolation_conflict"] is True
    assert len(ba.after.proof["adjustments"]) == 1
