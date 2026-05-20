"""MCP gateway front-end — a second driver for the same Pherix engine (Slice 8).

The gateway is *not* a new engine. It is a thin dispatcher: an external MCP
client (Claude Code, Aider, Cursor) speaks JSON-RPC 2.0 over stdio, discovers
the operator's registered ``@tool`` functions, and calls them — each call
wrapped in a Pherix transaction via :func:`pherix.core.runtime.agent_txn` (or
:func:`pherix.core.dry_run.dry_run` for speculative mode). The agent needs zero
Pherix code; it just speaks MCP.

The wire format is JSON-RPC 2.0; effect/result serialisation reuses
:func:`pherix.core.effects.strict_json_default` verbatim — the gateway adds no
new serialisation vocabulary. The gateway maps a handshake identity string to a
:class:`pherix.core.policy.Policy`; that is its *only* policy responsibility —
it is a policy *selector*, not a policy engine.

Public surface:

- :class:`PherixGateway` — holds adapters + per-identity policy config.
- :class:`MCPServer` — the JSON-RPC method handler (``handle(request) ->
  response``); speaks the tool-call subset (``initialize`` / ``tools/list`` /
  ``tools/call``) only.
- :class:`InProcessMCPClient` — an in-process client stub that drives a server
  without a subprocess. Co-developed with the server so the two cannot drift on
  the wire format; load-bearing for the offline parity tests.
- :func:`serve_stdio` — the stdio transport loop a real MCP client spawns.
"""

from pherix.frontends.proxy.client import InProcessMCPClient, MCPClientError
from pherix.frontends.proxy.gateway import PherixGateway
from pherix.frontends.proxy.server import (
    MCPError,
    MCPServer,
    PROTOCOL_VERSION,
)
from pherix.frontends.proxy.transport import serve_stdio

__all__ = [
    "PherixGateway",
    "MCPServer",
    "MCPError",
    "InProcessMCPClient",
    "MCPClientError",
    "serve_stdio",
    "PROTOCOL_VERSION",
]
