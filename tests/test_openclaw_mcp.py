"""Offline proof that OpenClaw's MCP tool calls would be Pherix-governed (B1).

OpenClaw consumes MCP servers; the Pherix gateway is one. We don't need a real
OpenClaw daemon to prove the governance: we drive the *same* gateway the
``openclaw.json`` snippet registers, through the in-process MCP client, with
OpenClaw's handshake identity — and assert that a reversible write commits and
is attributed, a destructive call is refused by policy (and journals nothing
APPLIED), and an *unrecognised* client hits the deny-all floor. The gateway
dispatches tools; it never calls a model, so this is fully offline.
"""

import importlib

import pytest

from pherix.core.tools import REGISTRY
from pherix.frontends.proxy import InProcessMCPClient, MCPClientError

from examples.dogfood.coding.openclaw import OPENCLAW_IDENTITY, gateway_config
from examples.dogfood.coding.openclaw.gateway_config import build_gateway

_TOOL_NAMES = ("add_task", "rename_task", "clear_tasks")


@pytest.fixture(autouse=True)
def _fresh_tools():
    # gateway_config registers its @tools at import time, and the global
    # registry rejects duplicates — so to give every test a clean, populated
    # registry we drop the three names then reload the module to re-run the
    # decorators. Torn down after so other suites start clean.
    for name in _TOOL_NAMES:
        REGISTRY._tools.pop(name, None)
    importlib.reload(gateway_config)
    yield
    for name in _TOOL_NAMES:
        REGISTRY._tools.pop(name, None)


def _client_for_openclaw():
    gateway = build_gateway()
    client = InProcessMCPClient(gateway)
    client.initialize(identity=OPENCLAW_IDENTITY)
    return gateway, client


def test_openclaw_identity_sees_the_governed_tools():
    _gateway, client = _client_for_openclaw()
    listed = {t["name"] for t in client.tool_descriptors()}
    assert {"add_task", "rename_task", "clear_tasks"} <= listed


def test_reversible_write_commits_and_is_attributed():
    gateway, client = _client_for_openclaw()
    structured = client.expect("add_task", {"title": "ship the demo"})
    assert structured["committed"] is True

    # The effect is journalled and attributed to OpenClaw's identity in the
    # shared audit — exactly as a library-driven txn would be.
    txn_id = structured["txn_id"]
    effects = gateway.audit.get_effects(txn_id)
    assert [e["tool"] for e in effects] == ["add_task"]
    assert effects[0]["status"] == "APPLIED"
    txn = gateway.audit.get_transaction(txn_id)
    assert txn["client_id"] == OPENCLAW_IDENTITY


def test_destructive_call_is_refused_by_policy():
    _gateway, client = _client_for_openclaw()
    # clear_tasks is denied for OpenClaw: the gateway returns a *successful*
    # JSON-RPC response carrying isError=true (an engine refusal, not a
    # transport fault), so the model reads it and adapts. Nothing commits.
    envelope = client.call_tool("clear_tasks", {})
    assert client.is_tool_error(envelope) is True
    structured = client.structured_of(envelope)
    assert structured["committed"] is False
    assert structured["pherix_error"]  # a machine-readable refusal code


def test_unknown_client_hits_the_deny_floor():
    gateway = build_gateway()
    client = InProcessMCPClient(gateway)
    # A client whose identity the operator never granted: the gateway's
    # default_policy is deny-all, so even the otherwise-allowed add_task is
    # refused. An unrecognised MCP client never runs more permissively.
    client.initialize(identity="some-unregistered-agent")
    with pytest.raises(MCPClientError):
        client.expect("add_task", {"title": "should be denied"})
