"""MCPServer — the JSON-RPC 2.0 method handler for the tool-call subset.

One :class:`MCPServer` backs one MCP session (one client connection). It holds
the session identity (recorded at ``initialize``) and dispatches:

- ``initialize`` — handshake. The client declares an identity; the server
  records it and the gateway selects the session policy. Returns the settled
  protocol version (the client's request when supported), server info, and
  capabilities.
- ``ping`` — liveness check; returns an empty result.
- ``tools/list`` — enumerate the operator's registered ``@tool`` functions from
  the global ``REGISTRY``, each as ``{name, description, inputSchema}``.
- ``tools/call`` — dispatch one named tool through a Pherix transaction
  (``agent_txn``), or speculatively (``dry_run``) when the client sets
  ``params["_pherix_dry_run"]``. Returns the MCP ``tools/call`` envelope
  (``content`` + ``isError`` + a Pherix ``structuredContent`` payload),
  serialised via ``strict_json_default``.

**Notifications** (JSON-RPC requests with no ``id`` — e.g.
``notifications/initialized``) are accepted and answered with *no response*,
per spec. **Errors** split two ways: protocol errors (unknown method/tool,
malformed params) return a JSON-RPC ``error`` envelope; tool/business failures
(policy denial, gate block, isolation conflict, a raised tool body) return a
*successful* ``tools/call`` envelope with ``isError: true`` so the agent reads
the reason and adapts.

Anything outside the subset (``resources/*``, ``prompts/*``, ``sampling/*``)
is explicitly out of scope and answered with METHOD_NOT_FOUND.

Engine discipline: the server *dispatches into* the engine. It opens the
context manager, calls the tool inside the with-block (the ``@tool`` wrapper
auto-routes through the active txn), and lets the context manager commit on
clean exit. It never reimplements snapshot/rollback/gate/isolation logic — that
all lives in ``pherix.core``.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from pherix.core.dry_run import dry_run
from pherix.core.effects import StagedResult, strict_json_default
from pherix.core.isolation import IsolationConflict
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import (
    CompensatorNotRegistered,
    GateBlocked,
    agent_txn,
)
from pherix.core.tools import REGISTRY

# The MCP protocol version this gateway prefers, plus the set it can speak. At
# handshake we echo the client's requested version when we support it (the spec
# expects the server to settle on a mutually-understood version) and otherwise
# fall back to our preferred one. The tool-call subset is stable across these
# revisions; the only version-sensitive feature we emit is ``structuredContent``
# on tool results, which older clients simply ignore as an unknown field.
PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18"}
)

# JSON-RPC 2.0 reserved error codes (https://www.jsonrpc.org/specification).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Pherix-specific application error codes live in the implementation-defined
# server range (-32000 .. -32099). One code per engine refusal so a client can
# branch on *why* a tool call did not commit without parsing the message.
POLICY_VIOLATION = -32001
GATE_BLOCKED = -32002
ISOLATION_CONFLICT = -32003
TOOL_NOT_FOUND = -32004
COMPENSATOR_NOT_REGISTERED = -32005
TOOL_RAISED = -32006


class MCPError(Exception):
    """A handler-internal error carrying a JSON-RPC error code + message.

    Raised inside a handler; :meth:`MCPServer.handle` catches it and renders
    the standard ``{"error": {"code", "message"}}`` envelope. Keeping the code
    on the exception lets each handler fail with the right semantic code
    (policy vs gate vs isolation vs not-found) at the point of failure.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _jsonify(value: Any) -> Any:
    """Round-trip a value through the audit/wire serialiser.

    The wire format IS the audit format: results travel through
    ``strict_json_default`` exactly as they would into an audit row. We
    serialise to a JSON string and parse back so the value the client receives
    is plain JSON-compatible data (dicts, lists, scalars) — no live Python
    objects leak across the wire boundary. Anything ``strict_json_default``
    refuses (an exotic tool return) surfaces as a TypeError, which the caller
    maps to an internal error rather than silently coercing.
    """
    return json.loads(json.dumps(value, default=strict_json_default))


# Engine refusals: a well-formed call the engine declined to commit. Per MCP
# these are tool-level outcomes (isError content), not protocol errors. The map
# gives each a stable slug + the application code a programmatic client can
# branch on without parsing the human-readable message.
_REFUSAL_CODES: dict[type, tuple[str, int]] = {
    PolicyViolation: ("policy_violation", POLICY_VIOLATION),
    GateBlocked: ("gate_blocked", GATE_BLOCKED),
    IsolationConflict: ("isolation_conflict", ISOLATION_CONFLICT),
    CompensatorNotRegistered: (
        "compensator_not_registered",
        COMPENSATOR_NOT_REGISTERED,
    ),
}
_REFUSALS = tuple(_REFUSAL_CODES)


def _ok_result(payload: dict) -> dict:
    """MCP ``tools/call`` success envelope wrapping a Pherix payload.

    ``content`` carries a text rendering (the JSON payload as a string) for
    clients that only read content blocks; ``structuredContent`` carries the
    same payload as data for clients that prefer it (ignored as an unknown
    field by pre-2025-06-18 clients). ``isError`` is false.
    """
    payload = _jsonify(payload)
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, separators=(",", ":"))}
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _refusal_result(exc: Exception, ctx: Any) -> dict:
    """MCP ``tools/call`` envelope for an engine refusal / raised tool.

    The call was well-formed; the engine declined to commit (or the tool body
    raised). MCP wants this as a *successful* response with ``isError: true`` so
    the agent reads the reason and adapts. ``structuredContent`` carries a
    stable ``pherix_error`` slug, its application ``code``, and
    ``committed: false``; ``ctx`` (the rolled-back transaction, or None)
    supplies the ``txn_id`` when one exists.
    """
    slug, code = _REFUSAL_CODES.get(type(exc), ("tool_raised", TOOL_RAISED))
    message = str(exc) if slug != "tool_raised" else f"{type(exc).__name__}: {exc}"
    payload: dict[str, Any] = {
        "committed": False,
        "pherix_error": slug,
        "code": code,
        "message": message,
    }
    if ctx is not None:
        payload["txn_id"] = ctx.txn_id
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }


class MCPServer:
    """Per-session JSON-RPC handler bound to one :class:`PherixGateway`."""

    def __init__(self, gateway: Any):
        self._gateway = gateway
        # Session identity, recorded at ``initialize``. ``None`` until the
        # handshake completes; ``tools/call`` before ``initialize`` therefore
        # runs under the gateway's default policy (the safe floor).
        self._identity: str | None = None
        self._initialized = False

    # -- public entry point ------------------------------------------------

    def handle(self, request: dict) -> dict | None:
        """Dispatch one JSON-RPC request dict, return one response dict or None.

        Returns a well-formed JSON-RPC 2.0 envelope for requests (those
        carrying an ``id``), echoing the ``id`` verbatim per spec. Returns
        ``None`` for **notifications** (requests with no ``id`` — e.g.
        ``notifications/initialized``): JSON-RPC forbids responding to a
        notification, so the transport writes nothing. Method handlers raise
        :class:`MCPError` (mapped to the error envelope) or return a ``result``
        dict (mapped to the success envelope).
        """
        # A JSON-RPC notification is a request with no ``id`` member. It must
        # never receive a response — not even an error one — so we detect it
        # first and short-circuit every return path below to None.
        is_notification = "id" not in request
        req_id = request.get("id")

        if request.get("jsonrpc") != "2.0" or "method" not in request:
            return None if is_notification else self._error(
                req_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request"
            )

        method = request["method"]
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return None if is_notification else self._error(
                req_id, INVALID_PARAMS, "params must be an object"
            )

        if is_notification:
            # Known lifecycle notifications (``notifications/initialized`` and
            # friends) are accepted as no-ops; unknown ones are ignored too.
            # Either way a notification gets no response.
            return None

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                # Liveness check (request with id, empty result) — part of the
                # MCP utilities every client may send; answering keeps the
                # session healthy.
                result = {}
            elif method == "tools/list":
                result = self._tools_list(params)
            elif method == "tools/call":
                result = self._tools_call(params)
            else:
                return self._error(
                    req_id,
                    METHOD_NOT_FOUND,
                    f"method {method!r} is not in the Pherix gateway tool-call "
                    f"subset (initialize, ping, tools/list, tools/call)",
                )
        except MCPError as exc:
            return self._error(req_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - last-resort JSON-RPC envelope
            # Any unexpected handler failure must still produce a valid
            # JSON-RPC error rather than crashing the transport loop.
            return self._error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # -- method handlers ---------------------------------------------------

    def _initialize(self, params: dict) -> dict:
        """Handshake: record the client's identity, return server info.

        The identity is read from ``params["clientInfo"]["name"]`` — the
        standard MCP field where a client names itself (e.g. ``"claude-code"``,
        ``"aider"``). A bare ``params["identity"]`` is accepted as a fallback
        so a minimal client can declare identity without the full clientInfo
        block. Identity drives policy selection for the whole session.
        """
        identity: str | None = None
        client_info = params.get("clientInfo")
        if isinstance(client_info, dict):
            name = client_info.get("name")
            if isinstance(name, str):
                identity = name
        if identity is None and isinstance(params.get("identity"), str):
            identity = params["identity"]

        self._identity = identity
        self._initialized = True
        # Settle on a version: echo the client's request when we speak it, else
        # offer our preferred one and let the client decide whether to proceed.
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in _SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "serverInfo": {"name": "pherix-gateway", "version": "0.1.0"},
            # Tool-call subset only: we advertise tools, nothing else.
            "capabilities": {"tools": {"listChanged": False}},
        }

    def _tools_list(self, params: dict) -> dict:
        """Enumerate registered ``@tool`` functions as MCP tool descriptors.

        Each descriptor is ``{name, description, inputSchema}``. The
        ``inputSchema`` is a permissive object schema derived from the tool's
        *public* signature (the injected adapter handle removed) — param names
        are listed under ``properties``; non-defaulted params are ``required``.
        Type inference is intentionally minimal: every param is typed as a
        permissive empty schema. The agent gets the param names, which is what
        it needs to construct a call.
        """
        tools = []
        for name in _registered_tool_names():
            spec = REGISTRY.get(name)
            tools.append(
                {
                    "name": spec.name,
                    "description": _tool_description(spec),
                    "inputSchema": _input_schema(spec),
                }
            )
        return {"tools": tools}

    def _tools_call(self, params: dict) -> dict:
        """Dispatch a named tool through a Pherix transaction.

        ``params = {"name": <tool>, "arguments": {...}}``. An optional
        ``params["_pherix_dry_run"]`` (bool) routes through
        :func:`pherix.core.dry_run.dry_run` instead of committing. The tool is
        called *inside* the engine context manager so the ``@tool`` wrapper
        auto-routes it through the active transaction; on clean exit the
        context manager commits (or, for dry-run, rolls back and populates
        ``ctx.result``).

        The result follows the MCP ``tools/call`` envelope: a ``content`` array
        plus ``isError`` and a Pherix ``structuredContent`` payload. Two error
        classes are kept distinct per the spec:

        - *Protocol* errors (unknown tool, malformed params) raise
          :class:`MCPError` and surface as a JSON-RPC ``error`` envelope — the
          request was ill-formed.
        - *Tool/business* failures (policy denial, gate block, isolation
          conflict, missing compensator, a tool body that raised) are NOT
          JSON-RPC errors: the call was well-formed, the engine simply refused
          to commit. They come back as a *successful* response with
          ``isError: true`` and the reason in ``content``, so the agent reads
          the refusal and adapts rather than seeing a transport fault. Either
          way the transaction has been rolled back — nothing committed.
        """
        name = params.get("name")
        if not isinstance(name, str):
            raise MCPError(INVALID_PARAMS, "tools/call requires a string 'name'")
        if name not in REGISTRY:
            raise MCPError(TOOL_NOT_FOUND, f"no registered tool named {name!r}")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise MCPError(INVALID_PARAMS, "'arguments' must be an object")

        is_dry_run = bool(params.get("_pherix_dry_run", False))
        policy = self._gateway.policy_for(self._identity)

        if is_dry_run:
            return self._dispatch_dry_run(name, arguments, policy)
        return self._dispatch_commit(name, arguments, policy)

    # -- dispatch lanes ----------------------------------------------------

    def _dispatch_commit(self, name: str, arguments: dict, policy: Policy) -> dict:
        """Open ``agent_txn``, fire the tool, commit on clean exit.

        A clean exit returns an ``isError: false`` content envelope. An engine
        refusal (policy / gate / isolation / missing compensator) or a raised
        tool body returns an ``isError: true`` content envelope carrying the
        reason and a machine-readable ``pherix_error`` code in
        ``structuredContent`` — not a JSON-RPC error.
        """
        wrapper = _tool_wrapper(name)
        # Pre-bind so the refusal payload can read txn_id even when the body
        # raised: ``with ... as ctx`` assigns ctx before running the body, so
        # ctx is the live (rolled-back) transaction in the except branches.
        ctx = None
        try:
            with self._open_txn(policy) as ctx:
                result = wrapper(**arguments)
            # The context manager has committed by here (clean exit). A
            # StagedResult means an irreversible effect was staged and fired at
            # commit — surface its effect_id; the real return value landed in
            # the audit journal at commit-time (the partial-order property).
        except _REFUSALS as exc:
            return _refusal_result(exc, ctx)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - the tool body raised
            return _refusal_result(exc, ctx)

        return _ok_result(
            {
                "txn_id": ctx.txn_id,
                "committed": True,
                "result": _serialise_call_result(result),
            }
        )

    def _dispatch_dry_run(self, name: str, arguments: dict, policy: Policy) -> dict:
        """Open ``dry_run``, fire the tool, return the DryRunResult shape.

        Policy denial during a dry-run does NOT raise (the engine captures the
        verdict into the result instead), so a denied dry-run is still a
        *successful* speculative report — ``isError`` is false and the
        dirtiness lives in ``is_clean`` / the verdicts. Only a genuine
        tool-body failure yields ``isError: true``.
        """
        wrapper = _tool_wrapper(name)
        try:
            with self._open_dry_run(policy) as ctx:
                wrapper(**arguments)
            result = ctx.result
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - the tool body raised
            return _refusal_result(exc, None)

        return _ok_result(
            {
                "txn_id": result.txn_id,
                "dry_run": True,
                "dry_run_result": _serialise_dry_run(result),
            }
        )

    # -- engine entry points -----------------------------------------------

    def _open_txn(self, policy: Policy):
        return agent_txn(
            self._gateway.adapters,
            policy=policy,
            audit=self._gateway.audit,
            client_id=self._identity,
        )

    def _open_dry_run(self, policy: Policy):
        return dry_run(
            self._gateway.adapters,
            policy=policy,
            audit=self._gateway.audit,
            client_id=self._identity,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }


# -- module-level pure helpers ---------------------------------------------


def _registered_tool_names() -> list[str]:
    """Enumerate registered tool names for ``tools/list`` (registration order)."""
    return REGISTRY.tool_names()


def _tool_wrapper(name: str):
    """Return the call-site wrapper for a registered tool.

    The ``@tool`` decorator stores ``wrapper.tool_spec`` on the returned
    wrapper but ``REGISTRY`` only holds the :class:`ToolSpec`. We rebuild a
    callable that routes through the active txn by invoking the spec's wrapped
    function via the same interception contract the wrapper uses: when an
    ``active_txn`` is set, ``record_tool_call`` is the entry point. The
    registry's wrapper isn't stored, so we call through the active context
    directly — identical to what the wrapper does.
    """
    # The decorated wrapper is what tool authors hold; the registry only keeps
    # the spec. Rather than require the wrapper, dispatch through the active
    # txn the same way the wrapper would: a tiny shim that defers to
    # record_tool_call. This keeps the gateway independent of how the operator
    # imported their tool function.
    spec = REGISTRY.get(name)

    def _call(**kwargs: Any) -> Any:
        from pherix.core.tools import active_txn

        ctx = active_txn.get()
        if ctx is None:
            # No active txn — run raw (matches the wrapper's passthrough). The
            # gateway always opens a txn first, so this branch is defensive.
            return spec.fn(**kwargs)
        return ctx.record_tool_call(spec.name, (), kwargs)

    return _call


def _tool_description(spec: Any) -> str:
    """First line of the tool's docstring, or a synthesised fallback."""
    doc = inspect.getdoc(spec.fn)
    if doc:
        return doc.strip().splitlines()[0]
    return f"Pherix tool {spec.name!r} on resource {spec.resource!r}."


def _input_schema(spec: Any) -> dict:
    """A permissive JSON-schema-ish object schema from the public signature.

    Param names land under ``properties`` (each as an unconstrained ``{}``
    schema — no type inference); params without a default are ``required``.
    ``*args`` / ``**kwargs`` params are skipped (no fixed name to advertise).
    """
    sig = spec.public_signature()
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        properties[param.name] = {}
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _serialise_call_result(result: Any) -> Any:
    """Render a tools/call return value for the wire.

    A :class:`StagedResult` carries only the deterministic effect_id (the real
    value lands in the audit journal at commit). Everything else round-trips
    through ``strict_json_default``.
    """
    if isinstance(result, StagedResult):
        return {"staged": True, "effect_id": result.effect_id}
    return _jsonify(result)


def _serialise_dry_run(result: Any) -> dict:
    """Render a :class:`DryRunResult` for the wire.

    Includes the four observation layers Slice 7 defined plus Stream B's
    per-resource ``state_diff`` (present iff the running engine populates it).
    Effects are rendered to plain dicts; verdicts to ``{allow, rule_name,
    tool, where, effect_index, reason}``.
    """
    out: dict[str, Any] = {
        "txn_id": result.txn_id,
        "is_clean": result.is_clean,
        "journal": [_serialise_effect(e) for e in result.journal],
        "would_have_fired": [
            _serialise_effect(e) for e in result.would_have_fired
        ],
        "verdicts": [_serialise_verdict(v) for v in result.policy_verdicts],
    }
    # Stream B adds DryRunResult.state_diff (per-resource structural diff).
    # Forward-compat: include it only when the running engine carries it.
    state_diff = getattr(result, "state_diff", None)
    if state_diff is not None:
        out["state_diff"] = _jsonify(state_diff)
    return out


def _serialise_effect(effect: Any) -> dict:
    """Plain-dict view of one journalled :class:`Effect` for the wire."""
    result = effect.result
    if isinstance(result, StagedResult):
        result_view: Any = {"staged": True, "effect_id": result.effect_id}
    elif result is None:
        result_view = None
    else:
        result_view = _jsonify(result)
    return {
        "effect_id": effect.effect_id,
        "index": effect.index,
        "tool": effect.tool,
        "args": _jsonify(effect.args),
        "resource": effect.resource,
        "reversible": effect.reversible,
        "status": effect.status.name,
        "result": result_view,
    }


def _serialise_verdict(verdict: Any) -> dict:
    """Plain-dict view of one :class:`PolicyVerdict` for the wire."""
    return {
        "allow": verdict.allow,
        "rule_name": verdict.rule_name,
        "tool": verdict.tool,
        "where": verdict.where,
        "effect_index": verdict.effect_index,
        "reason": verdict.reason,
    }
