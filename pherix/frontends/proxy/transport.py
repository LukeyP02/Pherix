"""stdio JSON-RPC transport — what a real MCP client spawns as a subprocess.

Framing choice: **line-delimited JSON** (one JSON-RPC object per line,
terminated by ``\\n``), not Content-Length headers. Rationale: line-delimited
is trivially testable in-process (a fixed-string round-trip), needs no header
parsing state machine, and the offline test suite never needs the
Content-Length framing a production LSP-style transport would use. The
documented trade-off: line-delimited cannot carry an embedded literal newline
in the JSON, so the loop emits compact (no-indent) JSON, which never contains
one. A future real-MCP-client integration that requires Content-Length framing
swaps this module alone — ``MCPServer.handle`` is framing-agnostic.

The loop is deliberately thin: read a line, parse it, hand the dict to
``server.handle``, write the response dict back as one line. A notification
(``server.handle`` returns ``None``) draws no output line, per JSON-RPC. All
transaction and policy logic lives behind ``handle``; this module only moves
bytes.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from pherix.frontends.proxy.server import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    MCPServer,
)


def serve_stdio(
    server: MCPServer,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Run the line-delimited JSON-RPC loop until EOF on ``stdin``.

    Each non-blank input line is parsed as a JSON-RPC request and dispatched
    through ``server.handle``; the response is written as one compact JSON line
    to ``stdout`` and flushed immediately (so an interactive client sees each
    reply without buffering). A line that fails to parse as JSON gets a
    PARSE_ERROR response with ``id: null`` — the spec's shape for an
    unidentifiable request. ``stdin`` / ``stdout`` are injectable for testing;
    they default to the process streams.
    """
    src = stdin if stdin is not None else sys.stdin
    dst = stdout if stdout is not None else sys.stdout

    for line in src:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(dst, _parse_error(str(exc)))
            continue
        if not isinstance(request, dict):
            _write(dst, _parse_error("top-level JSON value must be an object"))
            continue
        try:
            response = server.handle(request)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive on any fault
            # A notification (no ``id``) must get no response, even on fault.
            if "id" not in request:
                continue
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        # ``handle`` returns None for notifications — JSON-RPC forbids a reply.
        if response is not None:
            _write(dst, response)


def _write(dst: TextIO, response: dict) -> None:
    """Serialise one response dict as a single compact JSON line + flush."""
    dst.write(json.dumps(response, separators=(",", ":")) + "\n")
    dst.flush()


def _parse_error(detail: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": PARSE_ERROR, "message": f"parse error: {detail}"},
    }


__all__ = ["serve_stdio"]
