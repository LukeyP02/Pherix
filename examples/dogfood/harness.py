"""The real-agent harness — a thin Anthropic tool-use loop with Pherix in the path.

``run_agent`` opens an ``agent_txn`` (or ``dry_run``), runs a real model in a
tool-use loop *inside* it, and dispatches every ``tool_use`` the model emits to
the matching Pherix ``@tool``. Because the tools are ``@tool``-wrapped and the
loop runs inside the transaction, each call is journalled, snapshotted, policy-
checked and audited — the library's intended shape, driven by a real LLM rather
than a script.

Two design choices make this both honest and offline-testable:

- **The model adapts to refusals.** A ``PolicyViolation`` (stage-time deny) is
  fed back to the model as a ``tool_result`` *error*, not raised — so the agent
  sees "DENIED: ..." and tries something else, exactly as it would in
  production. The transaction is never corrupted by a denied call (nothing was
  journalled).
- **The Anthropic client is injectable.** ``run_agent(..., client=...)`` lets
  the offline test pass a mock with a canned ``tool_use`` sequence; the real
  ``anthropic`` SDK is imported *lazily* only when no client is supplied, so the
  pytest suite never imports it, needs no key, and stays fully offline. The
  ``pherix`` library itself imports none of this.

Default model is ``claude-sonnet-4-6`` — capable enough to make real decisions,
cheap enough to run agent loops repeatedly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pherix.core.audit import AuditJournal
from pherix.core.dry_run import dry_run
from pherix.core.effects import StagedResult
from pherix.core.isolation import IsolationConflict
from pherix.core.policy import Policy, PolicyViolation
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.transaction import TxnState

DEFAULT_MODEL = "claude-sonnet-4-6"

# Commit-time engine refusals: surfaced on AgentRun.error rather than crashing
# the caller. Stage-time denials are handled inline (fed back to the model).
_COMMIT_REFUSALS = (GateBlocked, IsolationConflict, PolicyViolation)


@dataclass
class AgentRun:
    """The product of one real-agent run — everything needed to judge the outcome.

    ``transcript`` is the full message list (system prompt excluded), including
    the model's tool calls and the ``tool_result`` blocks fed back to it.
    ``journal`` is ``ctx.txn.effects`` — the Pherix effect journal the run
    produced. ``audit`` is the journal's persistent handle (query it by
    ``txn_id`` / ``client_id``). ``final_state`` is the terminal
    :class:`TxnState`. ``dry_run_result`` carries the
    :class:`pherix.DryRunResult` when ``mode="dry_run"``. ``error`` holds a
    commit-time refusal — an engine one (gate / isolation / policy) or a
    caller-declared domain one (see ``run_agent``'s ``commit_refusals``) — if
    the transaction could not commit. The run still returns rather than raising,
    so the caller can inspect what happened.
    """

    transcript: list[dict]
    journal: list
    audit: AuditJournal
    txn_id: str
    final_state: TxnState
    dry_run_result: Any = None
    error: Exception | None = None
    stop_reason: str | None = None
    turns: int = 0


def run_agent(
    *,
    task: str,
    system: str,
    tools: list[Callable[..., Any]],
    adapters: dict[str, Any],
    policy: Policy | None = None,
    client_id: str | None = None,
    mode: str = "commit",
    isolation: Any = None,
    commit_refusals: tuple[type, ...] = (),
    model: str = DEFAULT_MODEL,
    max_turns: int = 20,
    max_tokens: int = 1024,
    client: Any = None,
    audit: AuditJournal | None = None,
) -> AgentRun:
    """Run a real agent on ``task`` with Pherix wrapping its tool calls.

    ``tools`` is a list of ``@tool``-decorated callables (each carries a
    ``.tool_spec``). ``adapters`` / ``policy`` / ``client_id`` are passed
    straight to ``agent_txn`` / ``dry_run``. ``mode`` is ``"commit"`` (the
    transaction commits on a clean loop exit) or ``"dry_run"`` (it rolls back
    and ``dry_run_result`` is populated). ``client`` is an Anthropic-compatible
    client; when ``None`` the real SDK is constructed lazily (needs a key).

    ``isolation`` (commit mode only) is the resolution policy passed to
    ``agent_txn`` — ``Abort`` / ``Retry`` / ``Serialize`` — for the concurrent
    dogfoods; ``dry_run`` takes no isolation (it never competes to commit).

    ``commit_refusals`` lets a caller declare *domain* exception types that
    should be captured onto ``AgentRun.error`` exactly like the engine's own
    commit-time refusals, instead of propagating. A domain tool that raises at
    commit-time (e.g. a staged smoke-test that fails inside the fire loop) is a
    first-class ``_partial_unwind`` path — capturing it lets the caller inspect
    the unwound ``AgentRun`` rather than wrap the call in try/except.
    """
    if mode not in ("commit", "dry_run"):
        raise ValueError(f"mode must be 'commit' or 'dry_run', got {mode!r}")
    if mode == "dry_run" and isolation is not None:
        raise ValueError(
            "isolation has no meaning in dry_run mode (a dry-run never commits, "
            "so it never competes for a conflict)"
        )

    policy = policy or Policy.allow_all()
    audit = audit or AuditJournal.in_memory()
    client = client or _default_client()

    tool_map = {w.tool_spec.name: w for w in tools}
    tool_defs = [_anthropic_tool_def(w.tool_spec) for w in tools]
    messages: list[dict] = [{"role": "user", "content": task}]

    state: dict[str, Any] = {"stop": None, "turns": 0}

    def _loop() -> None:
        for _ in range(max_turns):
            state["turns"] += 1
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tool_defs,
                messages=messages,
            )
            state["stop"] = getattr(resp, "stop_reason", None)
            blocks = list(resp.content)
            messages.append(
                {"role": "assistant", "content": [_block_to_dict(b) for b in blocks]}
            )
            tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                return
            results = [_dispatch(tu, tool_map) for tu in tool_uses]
            messages.append({"role": "user", "content": results})

    dry_result = None
    error: Exception | None = None
    capture = _COMMIT_REFUSALS + tuple(commit_refusals)
    try:
        if mode == "dry_run":
            with dry_run(
                adapters, policy=policy, audit=audit, client_id=client_id
            ) as ctx:
                _loop()
            dry_result = ctx.result
        else:
            with agent_txn(
                adapters,
                policy=policy,
                audit=audit,
                client_id=client_id,
                isolation=isolation,
            ) as ctx:
                _loop()
    except capture as exc:
        # A commit-time refusal — an engine one (gate / isolation / commit-time
        # policy) or a caller-declared domain one (``commit_refusals``). The
        # context manager has already unwound; capture it rather than crash.
        error = exc

    return AgentRun(
        transcript=messages,
        journal=list(ctx.txn.effects),
        audit=audit,
        txn_id=ctx.txn_id,
        final_state=ctx.txn.state,
        dry_run_result=dry_result,
        error=error,
        stop_reason=state["stop"],
        turns=state["turns"],
    )


# --- tool dispatch ---------------------------------------------------------


def _dispatch(tool_use: Any, tool_map: dict[str, Callable[..., Any]]) -> dict:
    """Call one tool through Pherix; render the outcome as a ``tool_result``.

    A ``PolicyViolation`` (stage-time deny) becomes an ``is_error`` result the
    model reads and adapts to — the denied call left nothing in the journal. Any
    other tool exception is likewise reported back rather than crashing the
    loop, so a single bad call doesn't abort the whole run.
    """
    name = getattr(tool_use, "name", None)
    tool_use_id = getattr(tool_use, "id", None)
    args = getattr(tool_use, "input", None) or {}
    wrapper = tool_map.get(name)
    if wrapper is None:
        return _tool_result(tool_use_id, f"unknown tool {name!r}", is_error=True)
    try:
        out = wrapper(**args)
    except PolicyViolation as exc:
        return _tool_result(tool_use_id, f"DENIED by policy: {exc}", is_error=True)
    except Exception as exc:  # noqa: BLE001 - report tool faults to the model
        return _tool_result(
            tool_use_id, f"tool error: {type(exc).__name__}: {exc}", is_error=True
        )
    return _tool_result(tool_use_id, _render_output(out))


def _render_output(out: Any) -> str:
    """A model-readable string for a tool's return value."""
    if isinstance(out, StagedResult):
        return (
            f"staged (irreversible; fires at commit) effect_id={out.effect_id}"
        )
    if isinstance(out, (str, int, float, bool)) or out is None:
        return str(out)
    try:
        return json.dumps(out, default=str)
    except (TypeError, ValueError):
        return str(out)


def _tool_result(tool_use_id: Any, content: str, *, is_error: bool = False) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


# --- Anthropic schema plumbing ---------------------------------------------


_TYPE_MAP = {
    int: "integer",
    float: "number",
    bool: "boolean",
    str: "string",
    bytes: "string",
}


def _anthropic_tool_def(spec: Any) -> dict:
    """Build an Anthropic tool definition from a Pherix ``ToolSpec``.

    The agent sees the *public* signature (the injected adapter handle removed).
    Param types are mapped from annotations where present, defaulting to string;
    params without a default are ``required``.
    """
    import inspect

    sig = spec.public_signature()
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        json_type = _TYPE_MAP.get(param.annotation, "string")
        properties[param.name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {
        "name": spec.name,
        "description": _description(spec),
        "input_schema": schema,
    }


def _description(spec: Any) -> str:
    import inspect

    doc = inspect.getdoc(spec.fn)
    if doc:
        return doc.strip().splitlines()[0]
    return f"Pherix tool {spec.name!r} on resource {spec.resource!r}."


def _block_to_dict(block: Any) -> dict:
    """Normalise a response content block to a plain dict for the transcript."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "input": getattr(block, "input", None) or {},
        }
    # Unknown block types pass through best-effort (forward-compat with new
    # Anthropic content kinds the loop doesn't act on).
    return {"type": btype, "raw": str(block)}


# --- real-run plumbing (never reached on the offline test path) ------------


def _default_client() -> Any:
    """Construct the real Anthropic client — lazy import, key from env / .env.

    Only called when ``run_agent`` got no ``client``. Tests always inject one,
    so neither the ``anthropic`` import nor the key read happens offline.
    """
    _load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Real agent runs need a key — put it "
            "in `.env` at the repo root (see `.env.example`). Offline tests must "
            "pass a mock `client=` instead of constructing the real one."
        )
    import anthropic  # examples-only dependency; pip install -e '.[dogfood]'

    return anthropic.Anthropic(api_key=key)


def _load_env(path: Path | None = None) -> None:
    """Tiny hand-rolled ``.env`` reader (no python-dotenv dependency).

    Reads ``KEY=VALUE`` lines from the repo-root ``.env``, ignoring blanks and
    ``#`` comments, and never overrides a value already in ``os.environ``.
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
