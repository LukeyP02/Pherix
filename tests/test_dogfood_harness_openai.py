"""Offline proof that the OpenAI-compatible path dispatches through Pherix
*identically* to the Anthropic path — Pherix's model-blindness, demonstrated.

No network, no key, no ``openai`` import: a mock chat-completions client emits a
canned ``tool_calls`` sequence (the Ollama / vLLM wire shape) and we assert the
harness journalled the same effect, wrote the same audit rows, committed the
same row, and honoured the policy the same way as ``test_dogfood_harness.py``
does for Anthropic. The two backends differ only in how a model request /
response is shaped on the wire; the Pherix dispatch behind them is the same
code, which is the whole point of the no-LLM-wrappers line — a local
open-source model is governed exactly as cloud Claude is.
"""

import sqlite3
from types import SimpleNamespace as NS

import pytest

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


# --- OpenAI-compatible response mocks (the chat-completions wire shape) -----


def _completion(*, content=None, tool_calls=None, finish_reason):
    msg = NS(content=content, tool_calls=tool_calls or None)
    choice = NS(message=msg, finish_reason=finish_reason)
    return NS(choices=[choice])


def _tool_call(call_id, name, arguments):
    # arguments is a JSON *string*, exactly as an OpenAI-compatible server sends.
    return NS(id=call_id, type="function", function=NS(name=name, arguments=arguments))


class _FakeOpenAIClient:
    """A scripted OpenAI-compatible client.

    ``chat.completions.create`` returns the canned completions in order and
    records each call's kwargs so a test can assert the harness passed tools /
    messages in the chat-completions shape.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls = []
        # client.chat.completions.create(...)
        self.chat = NS(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses[self._i]
        self._i += 1
        return resp


def test_openai_path_journals_tool_call_and_commits(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeOpenAIClient(
        [
            _completion(
                tool_calls=[_tool_call("call_1", "insert_user", '{"name": "bob"}')],
                finish_reason="tool_calls",
            ),
            _completion(content="done", finish_reason="stop"),
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
        client_id="harness-openai-test",
        api="openai",
    )

    assert isinstance(run, AgentRun)
    # Identical outcome to the Anthropic path: row persisted, journal + audit.
    assert _names(conn) == ["bob"]
    assert run.final_state is TxnState.COMMITTED
    assert [e.tool for e in run.journal] == ["insert_user"]
    assert run.journal[0].status.name == "APPLIED"
    txn = audit.get_transaction(run.txn_id)
    assert txn["state"] == "COMMITTED"
    assert txn["client_id"] == "harness-openai-test"
    effects = audit.get_effects(run.txn_id)
    assert [e["tool"] for e in effects] == ["insert_user"]
    assert run.turns == 2
    assert run.stop_reason == "stop"

    # The harness drove the chat-completions API in its native shape: system is
    # a leading message (not a kwarg), tools are function-typed.
    first_call = client.calls[0]
    assert "system" not in first_call
    assert first_call["messages"][0] == {"role": "system", "content": "You manage users."}
    assert first_call["tools"][0]["type"] == "function"
    assert first_call["tools"][0]["function"]["name"] == "insert_user"


def test_openai_path_feeds_policy_denial_back_to_the_model(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeOpenAIClient(
        [
            _completion(
                tool_calls=[_tool_call("call_1", "insert_user", '{"name": "mallory"}')],
                finish_reason="tool_calls",
            ),
            _completion(content="understood, stopping", finish_reason="stop"),
        ]
    )

    run = run_agent(
        task="add a user named mallory",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        policy=Policy(deny={"insert_user"}),
        client=client,
        api="openai",
    )

    # Denied call: nothing in the world, nothing journalled.
    assert _names(conn) == []
    assert run.journal == []
    # The denial came back as a role="tool" message the model reads and adapts
    # to — the OpenAI-path equivalent of the Anthropic is_error block.
    tool_msgs = [
        msg
        for msg in run.transcript
        if isinstance(msg, dict) and msg.get("role") == "tool"
    ]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "DENIED" in tool_msgs[0]["content"]


def test_openai_path_dry_run_previews_without_persisting(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeOpenAIClient(
        [
            _completion(
                tool_calls=[_tool_call("call_1", "insert_user", '{"name": "ghost"}')],
                finish_reason="tool_calls",
            ),
            _completion(content="previewed", finish_reason="stop"),
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
        api="openai",
    )

    assert _names(conn) == []
    assert run.final_state is TxnState.ROLLED_BACK
    assert run.dry_run_result is not None
    assert run.dry_run_result.is_clean is True
    assert [e.tool for e in run.dry_run_result.journal] == ["insert_user"]


def test_openai_path_reports_unknown_tool_to_the_model(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeOpenAIClient(
        [
            _completion(
                tool_calls=[_tool_call("call_1", "drop_database", "{}")],
                finish_reason="tool_calls",
            ),
            _completion(content="ok", finish_reason="stop"),
        ]
    )

    run = run_agent(
        task="do something",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        client=client,
        api="openai",
    )

    assert _names(conn) == []
    assert run.journal == []
    tool_msgs = [
        msg
        for msg in run.transcript
        if isinstance(msg, dict) and msg.get("role") == "tool"
    ]
    assert "unknown tool" in tool_msgs[0]["content"]


def test_openai_path_tolerates_malformed_tool_arguments(conn):
    # A local model can emit invalid JSON for a tool's arguments. The harness
    # degrades that to {} so the bad call surfaces as a tool error the model
    # reads (here: a missing required arg), rather than crashing the loop.
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    client = _FakeOpenAIClient(
        [
            _completion(
                tool_calls=[_tool_call("call_1", "insert_user", "{not valid json")],
                finish_reason="tool_calls",
            ),
            _completion(content="ok", finish_reason="stop"),
        ]
    )

    run = run_agent(
        task="add a user",
        system="You manage users.",
        tools=[insert_user],
        adapters={"sql": SQLiteAdapter(conn)},
        client=client,
        api="openai",
    )

    assert _names(conn) == []
    tool_msgs = [
        msg
        for msg in run.transcript
        if isinstance(msg, dict) and msg.get("role") == "tool"
    ]
    assert tool_msgs[0]["content"].startswith("tool error")


def test_unknown_api_is_rejected(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    with pytest.raises(ValueError, match="api must be"):
        run_agent(
            task="x",
            system="s",
            tools=[insert_user],
            adapters={"sql": SQLiteAdapter(conn)},
            client=_FakeOpenAIClient([]),
            api="gpt-9000",
        )
