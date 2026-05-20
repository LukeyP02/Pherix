"""InProcessMCPClient — an MCP client stub that drives a server without I/O.

CRITICAL for the offline test suite: the ``mcp`` Python SDK is not installed
and cannot be (the suite runs fully offline). This stub speaks the exact same
JSON-RPC dict envelope the stdio transport carries, but hands each request
directly to a server's :meth:`~pherix.frontends.proxy.server.MCPServer.handle`
in-process — no subprocess, no stdin/stdout. Because the client and server are
co-developed in this package, the two cannot drift on the wire format: every
request this stub builds is a request the real transport would frame, and every
response it parses is a response the real transport would emit.

Stream C imports this stub to write the load-bearing parity tests::

    from pherix.frontends.proxy import InProcessMCPClient

Return-value contract (fixed for Stream C): :meth:`initialize`,
:meth:`list_tools`, and :meth:`call_tool` all return the **full JSON-RPC
response envelope** — ``{"jsonrpc", "id", "result": {...}}`` on success or
``{"jsonrpc", "id", "error": {...}}`` on a refusal. The client does NOT raise on
an error envelope and does NOT unwrap ``result`` — the caller decides. This lets
a test treat a policy denial as data (inspect ``resp["error"]["code"]``) rather
than catch an exception, which is what the parity / per-identity tests need.
:meth:`result_of` and :meth:`error_of` are convenience accessors for the two
branches.
"""

from __future__ import annotations

from typing import Any

from pherix.frontends.proxy.gateway import PherixGateway
from pherix.frontends.proxy.server import MCPServer


class MCPClientError(Exception):
    """Raised by :meth:`InProcessMCPClient.expect` on an error envelope.

    Carries the ``code`` and ``message`` from the JSON-RPC error object so a
    test that *wants* exception semantics (``client.expect(...)``) can assert on
    the semantic code. The default ``call_tool`` path returns the envelope and
    never raises this — exception semantics are opt-in.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class InProcessMCPClient:
    """Drives a :class:`MCPServer` via the JSON-RPC dict envelope, in-process."""

    def __init__(self, target: PherixGateway | MCPServer):
        # Accept a gateway (construct our own per-session server) or an
        # existing server (share session state across calls). A gateway is the
        # common case; passing a server lets a test inspect server-side session
        # identity directly.
        if isinstance(target, MCPServer):
            self._server = target
        else:
            self._server = MCPServer(target)
        self._next_id = 0

    # -- the three methods (all return the full JSON-RPC envelope) ----------

    def initialize(self, identity: str | None = None) -> dict:
        """Send ``initialize``; the identity drives session policy selection.

        ``identity`` is sent as ``params["clientInfo"]["name"]`` — the standard
        MCP field. ``None`` sends no clientInfo, exercising the gateway's
        default-policy fallback for an anonymous client. Returns the response
        envelope.
        """
        params: dict[str, Any] = {"protocolVersion": "2025-06-18"}
        if identity is not None:
            params["clientInfo"] = {"name": identity, "version": "0.0.0"}
        resp = self._send("initialize", params)
        # Mirror the real MCP lifecycle: a client confirms readiness with the
        # ``notifications/initialized`` notification (no id, no response). This
        # also exercises the server's notification path through the stub.
        self._notify("notifications/initialized", {})
        return resp

    def list_tools(self) -> dict:
        """Send ``tools/list``; return the response envelope.

        Use :meth:`tool_descriptors` for the unwrapped ``result["tools"]``
        list.
        """
        return self._send("tools/list", {})

    def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        dry_run: bool = False,
    ) -> dict:
        """Send ``tools/call``; return the full response envelope.

        Returns the full JSON-RPC envelope. A protocol error (unknown tool,
        bad params) is an ``error`` envelope. A successful dispatch is a
        ``result`` envelope holding the MCP ``tools/call`` shape
        ``{"content": [...], "structuredContent": {...}, "isError": bool}``.
        ``isError`` is true for an engine refusal (policy / gate / isolation)
        or a raised tool body — the call was well-formed but did not commit.
        Use :meth:`structured_of` to reach the Pherix payload
        (``{"txn_id", "committed", "result"}`` on a commit, or
        ``{"txn_id", "dry_run", "dry_run_result"}`` for a dry-run, or
        ``{"committed": False, "pherix_error", "code", "message"}`` on a
        refusal).
        """
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if dry_run:
            params["_pherix_dry_run"] = True
        return self._send("tools/call", params)

    # -- convenience accessors ---------------------------------------------

    def tool_descriptors(self) -> list[dict]:
        """The unwrapped ``result["tools"]`` list from :meth:`list_tools`."""
        return self.list_tools()["result"]["tools"]

    @staticmethod
    def result_of(envelope: dict) -> Any:
        """The ``result`` payload of a success envelope (raises if it errored)."""
        if "error" in envelope:
            err = envelope["error"]
            raise MCPClientError(err["code"], err["message"])
        return envelope["result"]

    @staticmethod
    def error_of(envelope: dict) -> dict | None:
        """The JSON-RPC ``error`` object of an envelope, or ``None``.

        Note this is the *protocol*-error channel only (unknown tool, bad
        params). An engine refusal (policy / gate / isolation) is NOT a JSON-RPC
        error — it is a successful envelope with ``isError: true``; use
        :meth:`is_tool_error` / :meth:`structured_of` for that.
        """
        return envelope.get("error")

    @classmethod
    def structured_of(cls, envelope: dict) -> Any:
        """The Pherix ``structuredContent`` payload of a tools/call success.

        Unwraps the JSON-RPC ``result`` (raising :class:`MCPClientError` on a
        protocol-error envelope) then the MCP ``structuredContent``.
        """
        return cls.result_of(envelope)["structuredContent"]

    @classmethod
    def is_tool_error(cls, envelope: dict) -> bool:
        """Whether a tools/call envelope reports an engine refusal/raised tool.

        ``True`` when the result carries ``isError: true``. Raises
        :class:`MCPClientError` if the envelope is a JSON-RPC protocol error
        instead (a different failure channel).
        """
        return bool(cls.result_of(envelope).get("isError", False))

    def expect(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        dry_run: bool = False,
    ) -> Any:
        """Like :meth:`call_tool` but raise :class:`MCPClientError` on refusal.

        Convenience for tests that assert on the happy path or want exception
        semantics: returns the unwrapped ``structuredContent`` payload, raising
        :class:`MCPClientError` on a JSON-RPC protocol error AND on an engine
        refusal (``isError: true``) — both failure channels collapse to one
        exception.
        """
        envelope = self.call_tool(name, arguments, dry_run=dry_run)
        result = self.result_of(envelope)
        if result.get("isError", False):
            structured = result.get("structuredContent", {})
            raise MCPClientError(
                structured.get("code", -32000),
                structured.get("message", "tool call reported isError"),
            )
        return result["structuredContent"]

    # -- envelope plumbing -------------------------------------------------

    def _send(self, method: str, params: dict) -> dict:
        """Build a JSON-RPC request, dispatch it, return the response envelope."""
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        response = self._server.handle(request)
        # The id must echo per spec — a mismatch means the envelope contract
        # broke, which the parity tests want to catch loudly.
        assert response is not None and response.get("id") == self._next_id, (
            f"JSON-RPC id mismatch: sent {self._next_id}, got "
            f"{response.get('id') if response else None}"
        )
        return response

    def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no id) — the server must not respond."""
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        response = self._server.handle(request)
        assert response is None, (
            f"notification {method!r} drew a response: {response!r}"
        )


__all__ = ["InProcessMCPClient", "MCPClientError"]
