"""RESTAdapter — irreversible transport adapter + a REST/GraphQL tool harness.

The adapter itself is HTTPAdapter-shaped: an outbound REST call (or a GraphQL
mutation, which is just a POST) has no before-image, so it cannot be
snapshot-restored. ``supports_rollback() -> False`` routes its effects down the
staging lane — the call does not fire at stage-time, it is deferred to
``commit()`` and undone (if at all) via a registered compensator.

The *value over a bare HTTPAdapter* is the harness: :func:`rest_tool` and
:func:`graphql_tool` turn a SaaS endpoint into a Pherix tool in one call. They
build and register a ``@tool``-decorated function (``reversible=False,
injects_handle=False``) and optionally pair it with a compensator name. The
real network call sits behind an *injectable* transport callable
``transport(method, url, **kw) -> response`` so the harness is testable with no
network. If no transport is given, one is lazy-built over the standard library
(``urllib``) at fire-time — the kernel never imports an HTTP client at module
top.

This module never imports ``core/tools.py`` at the adapter layer; the harness
helpers do import the public ``@tool`` decorator, which is the same seam any
tool author uses.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.adapters.http import IrreversibleAdapterError
from pherix.core.effects import Effect
from pherix.core.tools import tool

# A transport is any callable with the shape requests/httpx already have:
#   transport(method, url, **kw) -> response-like
# We keep it duck-typed so a real client, a thin urllib shim, or a test fake
# all satisfy it without a structural dependency.
Transport = Callable[..., Any]


class RESTAdapter:
    """``ResourceAdapter`` over an external REST/GraphQL service (irreversible).

    Behaviourally identical to :class:`~pherix.core.adapters.http.HTTPAdapter`:
    it conforms to :class:`ResourceAdapter` only (no transaction-scope
    lifecycle), reports ``supports_rollback() -> False``, and fires the tool at
    commit-time with no injected handle. The difference is purely the harness
    that produces tools targeting it.
    """

    name = "rest"

    def supports_rollback(self) -> bool:
        return False

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        raise IrreversibleAdapterError(
            "RESTAdapter.snapshot() must not be called: a REST/GraphQL call has "
            "no before-image. Irreversible effects are staged and fired at "
            "commit-time; the runtime must never request a snapshot here."
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # No handle injected — REST tools declare injects_handle=False and own
        # the call themselves (via their bound transport). The adapter just
        # passes the journalled args through as kwargs.
        return tool_fn(**effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        raise IrreversibleAdapterError(
            "RESTAdapter.restore() must not be called: there is no before-state "
            "to restore. Unwind a fired REST effect via its compensator."
        )

    def read_version(self, key: tuple) -> object:
        raise IrreversibleAdapterError(
            "RESTAdapter.read_version() must not be called: irreversible effects "
            "are isolated-by-construction via staging — there is no version."
        )

    def write_version(self, key: tuple) -> object:
        raise IrreversibleAdapterError(
            "RESTAdapter.write_version() must not be called: see read_version."
        )


# --- the harness --------------------------------------------------------------


def _default_transport() -> Transport:
    """Build a stdlib-only transport, lazily, so no HTTP client is imported
    at module top. Returns a callable ``transport(method, url, **kw)``.

    Recognised keyword arguments: ``json`` (a dict, sent as a JSON body with
    the matching content-type), ``data`` (raw bytes/str body), ``headers``
    (a dict). The response is a small dict ``{"status", "headers", "body"}``
    with ``body`` JSON-decoded when possible, else the raw text.
    """
    import urllib.request  # lazy — never at module import time

    def transport(method: str, url: str, **kw: Any) -> Any:
        headers = dict(kw.get("headers") or {})
        body: bytes | None = None
        if "json" in kw and kw["json"] is not None:
            body = json.dumps(kw["json"]).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif kw.get("data") is not None:
            data = kw["data"]
            body = data.encode("utf-8") if isinstance(data, str) else data
        req = urllib.request.Request(
            url, data=body, method=method.upper(), headers=headers
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310 — caller-supplied URL is the tool's job to vet
            raw = resp.read().decode("utf-8")
            try:
                parsed: Any = json.loads(raw)
            except (ValueError, TypeError):
                parsed = raw
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": parsed,
            }

    return transport


def rest_tool(
    name: str,
    *,
    method: str,
    url: str,
    transport: Transport | None = None,
    compensator: str | None = None,
    resource: str = "rest",
) -> Callable[..., Any]:
    """Register and return an irreversible REST tool.

    The returned tool's public signature is ``(**kwargs)`` — any keyword args
    the agent passes (``json=``, ``data=``, ``headers=``, query params folded
    into ``url`` upstream, etc.) are forwarded verbatim to the transport. The
    journalled ``args`` are exactly those kwargs, which is also what the
    compensator receives (the runtime passes ``args=effect.args`` to the
    compensator), so a compensator paired here sees the same payload the
    original send did.

    ``transport`` is injectable for testing; if omitted, a stdlib transport is
    lazy-built at fire-time. ``compensator`` is the name of another registered
    tool that is this call's semantic inverse (e.g. a DELETE paired with a
    POST). Pherix resolves the name at fire-time and asserts its presence at
    stage-time; it does not verify the inverse property.
    """

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        name=name,
        compensator=compensator,
    )
    def _rest_call(**kwargs: Any) -> Any:
        active = transport if transport is not None else _default_transport()
        return active(method, url, **kwargs)

    return _rest_call


def graphql_tool(
    name: str,
    *,
    url: str,
    query: str,
    transport: Transport | None = None,
    compensator: str | None = None,
    resource: str = "rest",
) -> Callable[..., Any]:
    """Register and return an irreversible GraphQL tool.

    A GraphQL operation is just a POST of ``{"query": ..., "variables": ...}``
    to a single endpoint, so this is :func:`rest_tool` with the body shape
    fixed. The ``query`` (string) is bound at registration; the agent supplies
    ``variables`` (a dict) at call-time. A GraphQL *mutation* is irreversible
    in exactly the way a REST POST is — pair a compensating mutation via
    ``compensator`` to undo it.
    """

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        name=name,
        compensator=compensator,
    )
    def _graphql_call(variables: dict | None = None) -> Any:
        active = transport if transport is not None else _default_transport()
        return active(
            "POST", url, json={"query": query, "variables": variables or {}}
        )

    return _graphql_call
