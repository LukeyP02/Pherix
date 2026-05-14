"""@tool decorator + registry — transparent interception (D1).

A ``@tool``-decorated function returns a wrapper that checks a ContextVar for an
active transaction. Inside ``agent_txn()`` the call is journalled and routed
through an adapter; outside, the wrapper is a transparent passthrough and runs
the raw function un-journalled. The agent loop and tool call-sites are never
transaction-aware — there is no explicit ``txn.call()`` API.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable

# Set by runtime.agent_txn(). Holds the active transaction context — an object
# exposing `record_tool_call(tool_name, args, kwargs) -> result`. Typed loosely
# so tools.py never imports runtime.py (that would be an import cycle).
active_txn: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "pherix_active_txn", default=None
)


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    resource: str
    reversible: bool
    # First parameter (e.g. the SQL `conn`) is supplied by the adapter at apply
    # time and hidden from the agent's call-site — see D2.
    injects_handle: bool = True

    def public_signature(self) -> inspect.Signature:
        """The signature the agent sees — the injected handle removed."""
        sig = inspect.signature(self.fn)
        params = list(sig.parameters.values())
        if self.injects_handle:
            params = params[1:]
        return sig.replace(parameters=params)

    def bind_args(self, args: tuple, kwargs: dict) -> dict:
        """Resolve an agent call into a name->value dict for the journal."""
        bound = self.public_signature().bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def clear(self) -> None:
        self._tools.clear()


REGISTRY = ToolRegistry()


def tool(
    resource: str,
    *,
    reversible: bool = True,
    name: str | None = None,
    injects_handle: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolSpec(
            name=name or fn.__name__,
            fn=fn,
            resource=resource,
            reversible=reversible,
            injects_handle=injects_handle,
        )
        REGISTRY.register(spec)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = active_txn.get()
            if ctx is None:
                # Outside agent_txn(): transparent passthrough, un-journalled.
                return fn(*args, **kwargs)
            return ctx.record_tool_call(spec.name, args, kwargs)

        wrapper.tool_spec = spec  # introspection handle for the runtime / tests
        return wrapper

    return decorator
