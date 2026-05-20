"""Offline proof of the audit dogfood's composition — no key, no network.

Two mocked agents drive canned ``tool_use`` sequences under two ``client_id``s
against ONE on-disk SQLite ledger and ONE on-disk audit DB (each agent in its
own thread with its own connection and its own ``AuditJournal`` handle, exactly
as the real run does). We assert what the dogfood claims:

- both ``client_id``s appear attributed in the audit (``get_transaction``),
- no ledger corruption (the expected adjustment/flag rows are present, source
  entries intact),
- the per-client compliance view the dogfood builds is correct,
- and — the isolation payoff — when both agents race on the SAME entry row, the
  ``Abort`` policy unwinds the second committer rather than corrupting the ledger.

This tests the dogfood's COMPOSITION (tools + threading + view), not the engine
— the engine's isolation diff is already covered by the Slice 4 suite. The
Anthropic loop is mocked; nothing here imports ``anthropic`` or reads a key.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace as NS

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.isolation import IsolationConflict
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY
from pherix.core.transaction import TxnState

from examples.dogfood.audit import (
    _ACTIVE_CLIENT,
    AUDIT_TOOLS,
    LEDGER_SCHEMA,
    ClientRun,
    compliance_view,
    ledger_snapshot,
    post_adjustment,
    query_ledger,
    run_two_agents,
)
from examples.dogfood.infra import scratch_sqlite


# --- registry plumbing -----------------------------------------------------
#
# The autouse conftest fixture clears the process-global tool REGISTRY around
# every test, but the dogfood's three @tools register at module-IMPORT time
# (once). So we re-register their specs before each test in this file — the
# wrappers in AUDIT_TOOLS still carry their .tool_spec; we just put them back.


@pytest.fixture(autouse=True)
def _register_audit_tools():
    for wrapper in AUDIT_TOOLS:
        if wrapper.tool_spec.name not in REGISTRY:
            REGISTRY.register(wrapper.tool_spec)
    yield


# --- mock Anthropic client -------------------------------------------------


def _resp(*blocks, stop_reason):
    return NS(content=list(blocks), stop_reason=stop_reason)


def _tool_use(use_id, tool_name, inp=None):
    return NS(type="tool_use", id=use_id, name=tool_name, input=inp or {})


def _text(text):
    return NS(type="text", text=text)


class _FakeClient:
    """A scripted Anthropic-compatible client returning canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.messages = self

    def create(self, **kwargs):
        resp = self._responses[self._i]
        self._i += 1
        return resp


CLIENT_A = "auditor-a"
CLIENT_B = "auditor-b"


# --- tests -----------------------------------------------------------------


def test_two_agents_attributed_and_ledger_uncorrupted():
    """Disjoint work: each agent adjusts a different entry; both attributed."""
    audit_fd, audit_path = tempfile.mkstemp(suffix=".audit.db")
    os.close(audit_fd)
    try:
        with scratch_sqlite(schema=LEDGER_SCHEMA) as db:
            # A reads entry 1, then FLAGS it (a flag appends to the flags log;
            # it declares no write-key on the entry, so read-then-flag in one
            # txn commits clean). B posts an adjustment to entry 4 (write-only —
            # no preceding query of entry 4 in the same txn, which would
            # self-conflict; see the README's read-then-write-same-key note).
            clients = {
                CLIENT_A: _FakeClient(
                    [
                        _resp(
                            _tool_use("a1", "query_ledger", {"entry_id": 1}),
                            stop_reason="tool_use",
                        ),
                        _resp(
                            _tool_use(
                                "a2",
                                "flag_discrepancy",
                                {"entry_id": 1, "note": "needs review"},
                            ),
                            stop_reason="tool_use",
                        ),
                        _resp(_text("done"), stop_reason="end_turn"),
                    ]
                ),
                CLIENT_B: _FakeClient(
                    [
                        _resp(
                            _tool_use(
                                "b1",
                                "post_adjustment",
                                {"entry_id": 4, "delta": -50, "reason": "fx"},
                            ),
                            stop_reason="tool_use",
                        ),
                        _resp(_text("done"), stop_reason="end_turn"),
                    ]
                ),
            }
            tasks = {CLIENT_A: "reconcile entry 1", CLIENT_B: "reconcile entry 4"}

            # Sequential: deterministic ordering so the attribution / no-corruption
            # assertions don't ride on SQLite's concurrent write-lock timing. Each
            # agent still runs in its own thread with its own TxnContext, own
            # connection, and own AuditJournal to the shared audit file.
            runs = run_two_agents(
                db=db,
                audit_path=audit_path,
                tasks=tasks,
                clients=clients,
                sequential=True,
            )

            # Both agents committed cleanly (disjoint entries → no conflict).
            assert isinstance(runs[CLIENT_A], ClientRun)
            assert runs[CLIENT_A].run.final_state is TxnState.COMMITTED
            assert runs[CLIENT_B].run.final_state is TxnState.COMMITTED
            assert runs[CLIENT_A].run.error is None
            assert runs[CLIENT_B].run.error is None

            # Attribution: read the shared audit DB from a MAIN-THREAD handle
            # (each agent's own AuditJournal is thread-affine and already
            # closed). Each agent's txn row is present under its client_id.
            audit = AuditJournal(audit_path)
            try:
                for cid in (CLIENT_A, CLIENT_B):
                    txn_id = runs[cid].run.txn_id
                    txn = audit.get_transaction(txn_id)
                    assert txn is not None
                    assert txn["client_id"] == cid
                    assert txn["state"] == "COMMITTED"
            finally:
                audit.close()

            # No ledger corruption: source entries intact, both writes landed,
            # each attributed by the row's client_id column. Read through a
            # FRESH connection — the primary db.conn can hold a stale WAL read
            # snapshot from before the worker threads committed.
            entries = ledger_snapshot(db)
            assert {e["id"] for e in entries} == {1, 2, 3, 4}
            probe = db.connect()
            try:
                adj = probe.execute(
                    "SELECT entry_id, delta, client_id FROM adjustments"
                ).fetchall()
                flg = probe.execute(
                    "SELECT entry_id, note, client_id FROM flags"
                ).fetchall()
            finally:
                probe.close()
            assert adj == [(4, -50, CLIENT_B)]
            assert flg == [(1, "needs review", CLIENT_A)]

            # The per-client compliance view the dogfood builds is correct.
            views = compliance_view(
                audit_path=audit_path,
                ledger_db=db,
                client_ids=[CLIENT_A, CLIENT_B],
            )
            va, vb = views[CLIENT_A], views[CLIENT_B]
            assert len(va.txns) == 1 and va.txns[0]["client_id"] == CLIENT_A
            assert {e["tool"] for e in va.effects} == {
                "query_ledger",
                "flag_discrepancy",
            }
            assert va.adjustments == []
            assert va.flags == [
                {"id": 1, "entry_id": 1, "note": "needs review"}
            ]
            assert len(vb.txns) == 1 and vb.txns[0]["client_id"] == CLIENT_B
            assert vb.adjustments == [
                {"id": 1, "entry_id": 4, "delta": -50, "reason": "fx"}
            ]
            assert vb.flags == []
    finally:
        os.unlink(audit_path)


def test_two_reconcilers_on_same_entry_isolated_no_corruption():
    """Two reconcilers contend on entry 2 → isolation fires, ledger uncorrupted.

    Deterministic, in-process conflict (the reliable Slice-4 nested-``agent_txn``
    arbitration shape, driven through the DOGFOOD's own registered tools): A's
    txn reads entry 2; while A is open, B's nested txn adjusts entry 2 and
    commits, bumping the version side-table; A's commit-time diff then folds A's
    journal, sees A's read version moved, and ``Abort`` raises
    ``IsolationConflict`` — A unwinds. The ledger keeps exactly B's one
    adjustment; A's rolled-back read-only txn left nothing behind. Both txns are
    attributed by ``client_id`` in the shared on-disk audit.

    Why in-process and not two free-running threads: free concurrency on one
    SQLite file is genuinely racy (cross-connection WAL visibility lag can let a
    stale read commit clean ~3% of the time — a real engine finding recorded in
    the README). The in-process registry path is deterministic, so it is what we
    assert; the live ``__main__`` demo runs the threaded version where the race
    is acceptable (it is a demo, not a gate).
    """
    audit_fd, audit_path = tempfile.mkstemp(suffix=".audit.db")
    os.close(audit_fd)
    try:
        with scratch_sqlite(schema=LEDGER_SCHEMA) as db:
            conn_a = db.connect()
            conn_b = db.connect()
            conn_a.execute("PRAGMA busy_timeout = 5000")
            conn_b.execute("PRAGMA busy_timeout = 5000")
            audit = AuditJournal(audit_path)
            ad_a = SQLiteAdapter(conn_a)
            ad_b = SQLiteAdapter(conn_b)

            a_token = _ACTIVE_CLIENT.set(CLIENT_A)
            a_txn_id = b_txn_id = None
            conflict = None
            try:
                with agent_txn(
                    {"sql": ad_a}, audit=audit, client_id=CLIENT_A
                ) as ctx_a:
                    a_txn_id = ctx_a.txn_id
                    # A reads entry 2 (records read version into A's journal).
                    query_ledger(entry_id=2)
                    # While A is open, B adjusts entry 2 and commits — under its
                    # own client_id, bumping the ("entries", 2) version.
                    b_token = _ACTIVE_CLIENT.set(CLIENT_B)
                    try:
                        with agent_txn(
                            {"sql": ad_b}, audit=audit, client_id=CLIENT_B
                        ) as ctx_b:
                            b_txn_id = ctx_b.txn_id
                            post_adjustment(
                                entry_id=2, delta=-20, reason="race"
                            )
                    finally:
                        _ACTIVE_CLIENT.reset(b_token)
                    # A reaches end-of-block; auto-commit folds the diff →
                    # IsolationConflict (A's read of entry 2 is stale).
            except IsolationConflict as exc:
                conflict = exc
            finally:
                _ACTIVE_CLIENT.reset(a_token)

            # The conflict fired and A unwound; B committed.
            assert conflict is not None
            assert "isolation conflict" in str(conflict).lower()
            assert ctx_a.txn.state is not TxnState.COMMITTED
            assert ctx_b.txn.state is TxnState.COMMITTED

            # Ledger uncorrupted: exactly B's single adjustment row (read via a
            # fresh connection — a long-lived one can hold a stale WAL snapshot).
            probe = db.connect()
            try:
                adj = probe.execute(
                    "SELECT entry_id, delta, client_id FROM adjustments"
                ).fetchall()
            finally:
                probe.close()
            assert adj == [(2, -20, CLIENT_B)]

            # Both txns attributed in the shared audit, B committed, A unwound.
            assert audit.get_transaction(b_txn_id)["client_id"] == CLIENT_B
            assert audit.get_transaction(b_txn_id)["state"] == "COMMITTED"
            a_row = audit.get_transaction(a_txn_id)
            assert a_row["client_id"] == CLIENT_A
            assert a_row["state"] != "COMMITTED"

            audit.close()
            conn_a.close()
            conn_b.close()
    finally:
        os.unlink(audit_path)
