"""Governed-memory vocabulary — the standard tools + memory-specific policy.

Two halves, both proving the north-star claim that governed memory is *adapter +
policy*, not a new axis:

- :func:`register_memory_tools` ships the canonical ``remember`` / ``recall`` /
  ``forget`` tools as ordinary ``@tool``-decorated functions over the
  ``memory`` resource. Because they are plain registered tools, they appear in
  the MCP gateway's ``tools/list`` and run through ``tools/call`` with **no new
  front-end code** — the interception axis covers memory for free. They are
  registered via a factory (not at import) because the process-global tool
  registry is cleared between tests; a factory lets each caller register a fresh
  set against the namespace it wants.

- :func:`no_pii` and :func:`memory_byte_cap` are *ordinary Pherix policy*
  pointed at the memory resource. ``no_pii`` is a deny-rule (like
  :func:`~pherix.core.policy.refund_if_paid`); ``memory_byte_cap`` is literally
  :meth:`Cap.sum <pherix.core.policy.Cap.sum>` — memory growth caps need no new
  primitive. This is the policy axis covering memory with zero new vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from pherix.core.adapters.memory import MemoryHandle
from pherix.core.effects import Effect
from pherix.core.policy import Allow, Cap, Deny, Verdict, _SumCap
from pherix.core.tools import tool


@dataclass
class MemoryTools:
    """The three registered memory-tool wrappers returned by the factory."""

    remember: Callable[..., Any]
    recall: Callable[..., Any]
    forget: Callable[..., Any]


def register_memory_tools(
    *,
    remember_name: str = "remember",
    recall_name: str = "recall",
    forget_name: str = "forget",
) -> MemoryTools:
    """Register and return the standard memory tools over the ``memory`` resource.

    All three are reversible: the memory store is correct-by-construction
    rollback-able (savepoints), so ``supports_rollback()`` is honestly ``True``
    and these never take the staged/gated lane. ``recall`` records only a
    read_key — it is read-only by construction, so a policy that forbids memory
    writes leaves recall working without a special carve-out.

    Custom names let one process register several memory vocabularies (e.g. one
    per namespace) without colliding in the process-global registry.
    """

    @tool(resource="memory", name=remember_name)
    def remember(mem: MemoryHandle, key: str, value: Any) -> None:
        mem.remember(key, value)

    @tool(resource="memory", name=recall_name)
    def recall(mem: MemoryHandle, key: str) -> Any:
        return mem.recall(key)

    @tool(resource="memory", name=forget_name)
    def forget(mem: MemoryHandle, key: str) -> None:
        mem.forget(key)

    return MemoryTools(remember=remember, recall=recall, forget=forget)


# -- memory-specific policy --------------------------------------------------

# Default PII patterns: email, US SSN, and 13–16 digit card-like runs. The
# buyer brings their own edge patterns; this is the base any deployment assumes.
_DEFAULT_PII_PATTERNS: tuple[tuple[str, str], ...] = (
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("card", r"\b(?:\d[ -]?){13,16}\b"),
)


def no_pii(
    *,
    tools: tuple[str, ...] = ("remember",),
    value_arg: str = "value",
    patterns: tuple[tuple[str, str], ...] = _DEFAULT_PII_PATTERNS,
) -> Callable[[Effect, Any], Verdict]:
    """Deny remembering content that matches a PII pattern.

    Returns a rule ``(effect, ctx) -> Allow | Deny`` for ``policy.rule(...)`` /
    ``Policy.with_rules(rules=[...])``. It applies only to the named write tools
    (``remember`` by default); every other tool — crucially ``recall`` and
    ``forget``, which carry no new content — is a no-op ``Allow``. Because the
    runtime evaluates policy twice (stage-time and commit-time), the rule fires
    on both passes; nothing PII-bearing ever reaches the store.

    Non-string values are JSON-stringified for matching, so a PII string nested
    in a dict is still caught.
    """
    compiled = [(label, re.compile(pat)) for label, pat in patterns]

    def _rule(effect: Effect, ctx: Any) -> Verdict:
        if effect.tool not in tools:
            return Allow()
        if value_arg not in effect.args:
            return Allow()
        value = effect.args[value_arg]
        text = value if isinstance(value, str) else _json(value)
        for label, rx in compiled:
            if rx.search(text):
                return Deny(
                    f"no_pii: {effect.tool!r} value matches a {label} pattern; "
                    f"refusing to persist PII to memory"
                )
        return Allow()

    return _rule


def memory_byte_cap(
    *,
    max_bytes: int,
    tool: str = "remember",
    value_arg: str = "value",
) -> _SumCap:
    """Cap total bytes a transaction may remember — an ordinary :meth:`Cap.sum`.

    There is no memory-specific cap *primitive*: a growth cap is just a sum cap
    whose contribution is the byte length of each remembered value. This helper
    only spares the caller from writing the ``via`` extractor by hand — proof
    that the cap machinery already covers memory growth.
    """

    def _via(args: dict) -> int:
        value = args.get(value_arg, "")
        text = value if isinstance(value, str) else _json(value)
        return len(text.encode("utf-8"))

    return Cap.sum(tool=tool, via=_via, max=max_bytes)


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
