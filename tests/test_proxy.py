"""Stream A gateway-level tests — the MCP front-end driving the same engine.

These exercise the gateway mechanics through the in-process client stub (no
subprocess, no network — the suite is fully offline). Stream C layers the
deeper library-vs-gateway parity tests on top of the same
:class:`InProcessMCPClient`.
"""

import sqlite3

import pytest

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import tool
from pherix.frontends.proxy import (
    InProcessMCPClient,
    MCPServer,
    PherixGateway,
)
from pherix.frontends.proxy.server import (
    METHOD_NOT_FOUND,
    POLICY_VIOLATION,
    TOOL_NOT_FOUND,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def _names(conn):
    return [r[0] for r in conn.execute("SELECT name FROM users ORDER BY id")]


@pytest.fixture
def insert_user(conn):
    @tool(resource="sql")
    def insert_user(conn, name):
        """Insert a user row by name."""
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        return name

    return insert_user


@pytest.fixture
def adapters(conn):
    return {"sql": SQLiteAdapter(conn)}


# -- initialize handshake --------------------------------------------------


def test_initialize_returns_protocol_and_server_info(adapters):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    client = InProcessMCPClient(gw)
    result = client.initialize("claude-code")["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "pherix-gateway"
    assert "tools" in result["capabilities"]


def test_initialize_records_identity_for_policy_selection(adapters):
    # Known identity "claude-code" maps to allow_all; the session must select
    # that policy, not the (here-deny-everything) default.
    gw = PherixGateway(
        adapters=adapters,
        policies={"claude-code": Policy.allow_all()},
        default_policy=Policy(allow=set()),
    )
    server = MCPServer(gw)
    client = InProcessMCPClient(server)
    client.initialize("claude-code")
    assert gw.policy_for("claude-code") is gw.policies["claude-code"]
    # The server recorded the identity so subsequent calls select the policy.
    assert server._identity == "claude-code"


# -- tools/list ------------------------------------------------------------


def test_tools_list_returns_registered_tools(adapters, insert_user):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    client = InProcessMCPClient(gw)
    client.initialize("claude-code")
    tools = client.tool_descriptors()
    names = [t["name"] for t in tools]
    assert "insert_user" in names
    spec = next(t for t in tools if t["name"] == "insert_user")
    # The injected `conn` handle is hidden; only the agent-facing param remains.
    assert "name" in spec["inputSchema"]["properties"]
    assert "conn" not in spec["inputSchema"]["properties"]
    assert spec["inputSchema"]["required"] == ["name"]
    assert spec["description"] == "Insert a user row by name."


# -- tools/call: reversible commit -----------------------------------------


def test_tools_call_commits_reversible_write(conn, adapters, insert_user):
    audit = AuditJournal.in_memory()
    gw = PherixGateway(
        adapters=adapters, default_policy=Policy.allow_all(), audit=audit
    )
    client = InProcessMCPClient(gw)
    client.initialize("claude-code")
    resp = client.call_tool("insert_user", {"name": "bob"})
    # MCP tools/call success envelope: a content array + isError false.
    result = client.result_of(resp)
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    out = client.structured_of(resp)
    assert out["committed"] is True
    assert out["result"] == "bob"
    # The row persists — the transaction committed through the real engine.
    assert _names(conn) == ["bob"]
    # The audit journal recorded the committed transaction.
    txn = audit.get_transaction(out["txn_id"])
    assert txn["state"] == "COMMITTED"


def test_unknown_tool_is_a_jsonrpc_error(adapters):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    client = InProcessMCPClient(gw)
    client.initialize("claude-code")
    resp = client.call_tool("does_not_exist", {})
    assert resp["error"]["code"] == TOOL_NOT_FOUND


def test_unknown_method_is_method_not_found(adapters):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    server = MCPServer(gw)
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}}
    )
    assert resp["error"]["code"] == METHOD_NOT_FOUND
    assert resp["id"] == 7


# -- identity-driven policy selection (both directions) --------------------


def test_unknown_identity_falls_back_to_default_policy(conn, adapters, insert_user):
    # Default policy denies everything; an unknown identity inherits it and the
    # write is refused — nothing commits.
    gw = PherixGateway(
        adapters=adapters,
        policies={"claude-code": Policy.allow_all()},
        default_policy=Policy(allow=set()),
    )
    client = InProcessMCPClient(gw)
    client.initialize("some-random-client")
    resp = client.call_tool("insert_user", {"name": "bob"})
    # A policy denial is a tool-level refusal (isError content), not a
    # JSON-RPC protocol error — the call was well-formed, the engine refused.
    assert client.is_tool_error(resp) is True
    assert client.structured_of(resp)["code"] == POLICY_VIOLATION
    assert _names(conn) == []


def test_anonymous_identity_falls_back_to_default_policy(conn, adapters, insert_user):
    # A client that sent no identity at all (initialize without clientInfo)
    # also gets the default policy.
    gw = PherixGateway(
        adapters=adapters,
        policies={"claude-code": Policy.allow_all()},
        default_policy=Policy(allow=set()),
    )
    client = InProcessMCPClient(gw)
    client.initialize(None)
    resp = client.call_tool("insert_user", {"name": "bob"})
    assert client.is_tool_error(resp) is True
    assert client.structured_of(resp)["code"] == POLICY_VIOLATION
    assert _names(conn) == []


def test_tool_denied_for_one_identity_allowed_for_another(conn, adapters, insert_user):
    # The load-bearing pin in BOTH directions: "insert_user" is deny-listed for
    # aider but allowed for claude-code. Same gateway, same tool, same engine —
    # only the identity-selected policy differs.
    gw = PherixGateway(
        adapters=adapters,
        policies={
            "claude-code": Policy.allow_all(),
            "aider": Policy(deny={"insert_user"}),
        },
        default_policy=Policy(allow=set()),
    )

    # aider: denied (tool-level refusal — isError content).
    aider = InProcessMCPClient(gw)
    aider.initialize("aider")
    resp = aider.call_tool("insert_user", {"name": "blocked"})
    assert aider.is_tool_error(resp) is True
    assert aider.structured_of(resp)["code"] == POLICY_VIOLATION
    assert _names(conn) == []

    # claude-code: allowed, and it commits.
    cc = InProcessMCPClient(gw)
    cc.initialize("claude-code")
    out = cc.structured_of(cc.call_tool("insert_user", {"name": "allowed"}))
    assert out["committed"] is True
    assert _names(conn) == ["allowed"]


# -- dry-run lane ----------------------------------------------------------


def test_dry_run_does_not_persist_but_returns_journal(conn, adapters, insert_user):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    client = InProcessMCPClient(gw)
    client.initialize("claude-code")
    out = client.structured_of(
        client.call_tool("insert_user", {"name": "ghost"}, dry_run=True)
    )
    assert out["dry_run"] is True
    dr = out["dry_run_result"]
    assert dr["is_clean"] is True
    # The journal materialised (the fold happened) but the world is untouched.
    tools_in_journal = [e["tool"] for e in dr["journal"]]
    assert "insert_user" in tools_in_journal
    assert _names(conn) == []


def test_dry_run_captures_policy_denial_without_raising(conn, adapters, insert_user):
    # Under a deny-all default, a dry-run does NOT raise — the verdict is
    # captured into the result (Slice 7 capture-mode), and nothing persists.
    gw = PherixGateway(
        adapters=adapters, default_policy=Policy(allow=set())
    )
    client = InProcessMCPClient(gw)
    client.initialize("anonymous")
    out = client.structured_of(
        client.call_tool("insert_user", {"name": "ghost"}, dry_run=True)
    )
    dr = out["dry_run_result"]
    assert dr["is_clean"] is False
    assert any(v["allow"] is False for v in dr["verdicts"])
    assert _names(conn) == []


# -- transport framing (line-delimited JSON) -------------------------------


def test_stdio_transport_round_trips_a_request(adapters, insert_user):
    import io

    from pherix.frontends.proxy import serve_stdio

    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    server = MCPServer(gw)
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"clientInfo":{"name":"claude-code"}}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
        '{"name":"insert_user","arguments":{"name":"wired"}}}\n'
    )
    stdout = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout)

    import json

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    init_resp = json.loads(lines[0])
    call_resp = json.loads(lines[1])
    assert init_resp["result"]["serverInfo"]["name"] == "pherix-gateway"
    structured = call_resp["result"]["structuredContent"]
    assert structured["committed"] is True
    assert structured["result"] == "wired"


def test_stdio_transport_reports_parse_error_on_garbage(adapters):
    import io
    import json

    from pherix.frontends.proxy import serve_stdio
    from pherix.frontends.proxy.server import PARSE_ERROR

    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    server = MCPServer(gw)
    stdin = io.StringIO("this is not json\n")
    stdout = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout)
    resp = json.loads(stdout.getvalue().strip())
    assert resp["error"]["code"] == PARSE_ERROR
    assert resp["id"] is None


# -- MCP protocol conformance ----------------------------------------------


def test_initialize_echoes_a_supported_requested_protocol_version(adapters):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    server = MCPServer(gw)
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    # The server speaks 2024-11-05, so it settles on the client's request
    # rather than forcing its own preferred version.
    assert resp["result"]["protocolVersion"] == "2024-11-05"


def test_notifications_get_no_response(adapters):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    server = MCPServer(gw)
    # A notification (no "id") must never draw a response, per JSON-RPC.
    assert (
        server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        is None
    )
    # Even an unknown notification is silently accepted, never error-replied.
    assert (
        server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"})
        is None
    )


def test_ping_returns_empty_result(adapters):
    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    server = MCPServer(gw)
    resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}})
    assert resp["id"] == 9
    assert resp["result"] == {}


def test_tool_body_failure_is_iserror_content_not_jsonrpc_error(adapters, conn):
    @tool(resource="sql")
    def boom(conn):
        """A tool whose body raises."""
        raise RuntimeError("kaboom")

    gw = PherixGateway(adapters=adapters, default_policy=Policy.allow_all())
    client = InProcessMCPClient(gw)
    client.initialize("claude-code")
    resp = client.call_tool("boom", {})
    # A raised tool body is a tool-level error (isError content), not a
    # JSON-RPC protocol error — the agent should see the failure and adapt.
    assert client.error_of(resp) is None
    assert client.is_tool_error(resp) is True
    structured = client.structured_of(resp)
    assert structured["pherix_error"] == "tool_raised"
    assert "kaboom" in structured["message"]
    assert _names(conn) == []
