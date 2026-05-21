"""Mechanism test (mocked client, deterministic, CI) for the dogfood harness.

This is NOT a real-agent run. No network, no key, no ``anthropic`` import: a mock
client emits a canned ``tool_use`` sequence and we assert the harness journalled
the effects, wrote the audit rows, honoured the policy, and fed a denied call
back to the model as a ``tool_result`` error. This is the foundation the four
dogfood streams build on — if the harness mis-wires the loop, every dogfood is
suspect. The genuinely autonomous runs are the operator-invoked ``python -m
examples.dogfood.*`` scripts; these tests guard the wiring underneath them.
"""

import sqlite3
from types import SimpleNamespace as NS

import pytest

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

from examples.dogfood.harness import AgentRun, run_agent


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def _names(conn):
    return [r[0] for r in conn.execute("SELECT name FROM users ORDER BY id")]


def _resp(*blocks, stop_reason):
    return NS(content=list(blocks), stop_reason=stop_reason)


def _tool_use(use_id, tool_name, inp=None):
    return NS(type="tool_use", id=use_id, name=tool_name, input=inp or {})


def _text(text):
    return NS(type="text", text=text)


class _FakeClient:
    """A scripted Anthropic-compatible client.

    ``messages.create`` returns the canned responses in order and records each
    call's kwargs so a test can assert the harness passed tools / messages.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses[self._i]
        self._i += 1
        return resp


def test_harness_journals_tool_call_and_commits(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeClient(
        [
            _resp(_tool_use("tu_1", "insert_user", {"name": "bob"}), stop_reason="tool_use"),
            _resp(_text("done"), stop_reason="end_turn"),
        ]
    )
    audit = AuditJournal.in_memory()

    run = run_agent(
        task="add a user named bob",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        policy=Policy.allow_all(),
        client=client,
        audit=audit,
        client_id="harness-test",
    )

    assert isinstance(run, AgentRun)
    # The tool ran through the engine: row persisted, journal + audit recorded.
    assert _names(conn) == ["bob"]
    assert run.final_state is TxnState.COMMITTED
    assert [e.tool for e in run.journal] == ["insert_user"]
    assert run.journal[0].status.name == "APPLIED"
    txn = audit.get_transaction(run.txn_id)
    assert txn["state"] == "COMMITTED"
    assert txn["client_id"] == "harness-test"
    effects = audit.get_effects(run.txn_id)
    assert [e["tool"] for e in effects] == ["insert_user"]
    # The model was driven for two turns (tool_use, then end_turn).
    assert run.turns == 2
    assert run.stop_reason == "end_turn"


def test_harness_feeds_policy_denial_back_to_the_model(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeClient(
        [
            _resp(
                _tool_use("tu_1", "insert_user", {"name": "mallory"}),
                stop_reason="tool_use",
            ),
            _resp(_text("understood, stopping"), stop_reason="end_turn"),
        ]
    )

    run = run_agent(
        task="add a user named mallory",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        policy=Policy(deny={"insert_user"}),
        client=client,
    )

    # The denied call left nothing in the world and nothing journalled.
    assert _names(conn) == []
    assert run.journal == []
    # The denial was reported to the model as a tool_result error, not raised.
    tool_results = [
        block
        for msg in run.transcript
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True
    assert "DENIED" in tool_results[0]["content"]


def test_harness_dry_run_previews_without_persisting(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeClient(
        [
            _resp(
                _tool_use("tu_1", "insert_user", {"name": "ghost"}),
                stop_reason="tool_use",
            ),
            _resp(_text("previewed"), stop_reason="end_turn"),
        ]
    )

    run = run_agent(
        task="preview adding ghost",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        policy=Policy.allow_all(),
        client=client,
        mode="dry_run",
    )

    # Dry-run: the fold happened (journal materialised) but the world is clean.
    assert _names(conn) == []
    assert run.final_state is TxnState.ROLLED_BACK
    assert run.dry_run_result is not None
    assert run.dry_run_result.is_clean is True
    assert [e.tool for e in run.dry_run_result.journal] == ["insert_user"]


def test_harness_reports_unknown_tool_to_the_model(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeClient(
        [
            _resp(_tool_use("tu_1", "drop_database"), stop_reason="tool_use"),
            _resp(_text("ok"), stop_reason="end_turn"),
        ]
    )

    run = run_agent(
        task="do something",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        client=client,
    )

    # An unregistered tool is reported back, not crashed on; nothing committed.
    assert _names(conn) == []
    assert run.journal == []
    tool_results = [
        block
        for msg in run.transcript
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results[0]["is_error"] is True
    assert "unknown tool" in tool_results[0]["content"]


class _Boom(RuntimeError):
    """A domain failure raised by a tool at commit-time (fire loop)."""


def _explode_tools():
    @tool(resource="http", reversible=False, injects_handle=False, compensator="noop")
    def explode():
        """Irreversible tool that fails when it fires at commit-time."""
        raise _Boom("commit-time boom")

    @tool(resource="http", reversible=False, injects_handle=False)
    def noop():
        """No-op compensator (only present so explode clears the gate)."""
        return "noop"

    return [explode]


def _explode_client():
    return _FakeClient(
        [
            _resp(_tool_use("tu_1", "explode", {}), stop_reason="tool_use"),
            _resp(_text("done"), stop_reason="end_turn"),
        ]
    )


def test_commit_refusals_captures_a_domain_commit_failure():
    # A domain tool that raises at commit-time is captured onto AgentRun.error
    # (like the engine's own refusals) when declared in commit_refusals — the
    # caller inspects the unwound run instead of catching the exception.
    run = run_agent(
        task="explode at commit",
        system="s",
        tools=_explode_tools(),
        adapters={"http": HTTPAdapter()},
        client=_explode_client(),
        commit_refusals=(_Boom,),
    )
    assert isinstance(run.error, _Boom)
    assert run.final_state is TxnState.ROLLED_BACK
    # The real journal came back: the staged explode is recorded, FAILED.
    assert [e.tool for e in run.journal] == ["explode"]
    assert run.journal[0].status.name == "FAILED"


def test_undeclared_commit_failure_propagates():
    # Without declaring it, a domain commit-time raise is NOT swallowed — it
    # propagates, so a caller can't silently miss a real failure.
    with pytest.raises(_Boom):
        run_agent(
            task="explode at commit",
            system="s",
            tools=_explode_tools(),
            adapters={"http": HTTPAdapter()},
            client=_explode_client(),
        )


def test_isolation_in_dry_run_mode_is_rejected(conn):
    from pherix.core.isolation import Abort

    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    # isolation is meaningless for a dry-run (it never commits), so the harness
    # rejects the combination loudly rather than silently ignoring it.
    with pytest.raises(ValueError, match="dry_run"):
        run_agent(
            task="x",
            system="s",
            tools=[insert_user],
            adapters={"sql": SQLiteAdapter(conn)},
            client=_FakeClient([_resp(_text("noop"), stop_reason="end_turn")]),
            mode="dry_run",
            isolation=Abort(),
        )
