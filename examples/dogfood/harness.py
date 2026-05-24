"""The real-agent harness — a thin tool-use loop with Pherix in the path.

``run_agent`` opens an ``agent_txn`` (or ``dry_run``), runs a real model in a
tool-use loop *inside* it, and dispatches every tool call the model emits to
the matching Pherix ``@tool``. Because the tools are ``@tool``-wrapped and the
loop runs inside the transaction, each call is journalled, snapshotted, policy-
checked and audited — the library's intended shape, driven by a real LLM rather
than a script.

Three design choices make this honest, offline-testable, and model-blind:

- **The model adapts to refusals.** A ``PolicyViolation`` (stage-time deny) is
  fed back to the model as a tool-result *error*, not raised — so the agent
  sees "DENIED: ..." and tries something else, exactly as it would in
  production. The transaction is never corrupted by a denied call (nothing was
  journalled).
- **The client is injectable.** ``run_agent(..., client=...)`` lets the offline
  test pass a mock with a canned tool-call sequence; the real SDK is imported
  *lazily* only when no client is supplied, so the pytest suite never imports
  it, needs no key, and stays fully offline. The ``pherix`` library itself
  imports none of this.
- **The chat protocol sits behind a backend seam.** ``api="anthropic"`` drives
  the Anthropic Messages API; ``api="openai"`` drives any OpenAI-compatible
  chat-completions endpoint (Ollama, vLLM, LM Studio, …) via ``base_url``. The
  *only* thing that differs between the two is how a model request/response is
  shaped on the wire — the Pherix dispatch (``tool_map[name](**args)`` into the
  same ``@tool`` wrappers, the same journal, the same policy) is byte-identical.
  That identity *is* Pherix's model-blindness: a local open-source model on a
  local endpoint is governed exactly as cloud Claude is. ``tests/
  test_dogfood_harness_openai.py`` proves the two paths dispatch identically.

Default model is ``claude-sonnet-4-6`` (the Anthropic path) — capable enough to
make real decisions, cheap enough to run agent loops repeatedly. For the OpenAI
path the operator passes their local model id (e.g. ``"qwen2.5-coder:7b"``).
"""

from __future__ import annotations

import json
import os
import time
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

# --- rate-limit / overload backoff -----------------------------------------
#
# A real batch run makes many model calls; a transient 429 (rate limit) or 529
# (overload) must not kill the run (the last batch crashed on exactly this).
# We detect it backend-agnostically — by exception *type name* (the Anthropic
# and OpenAI SDKs both raise ``RateLimitError``; Anthropic adds
# ``OverloadedError``) or by an HTTP ``status_code`` of 429/529 — so neither SDK
# is imported here. A mock client never raises these, so the offline suite is
# unaffected unless a test deliberately injects one.
_BACKOFF_TYPE_NAMES = frozenset({"RateLimitError", "OverloadedError"})
_BACKOFF_STATUS = frozenset({429, 529})
_MAX_RETRIES = 5
_BASE_DELAY = 1.0  # seconds; doubles each attempt (1, 2, 4, …), capped


def _is_rate_limit(exc: BaseException) -> bool:
    """True if ``exc`` is a transient rate-limit / overload worth retrying.

    Type-name + status-code matching keeps this backend-agnostic: it recognises
    the Anthropic and OpenAI SDK errors without importing either, and ignores
    every other exception (a real bug must still surface, not be retried).
    """
    if type(exc).__name__ in _BACKOFF_TYPE_NAMES:
        return True
    return getattr(exc, "status_code", None) in _BACKOFF_STATUS


def _create_with_backoff(
    make_call: Callable[[], Any],
    *,
    max_retries: int = _MAX_RETRIES,
    base_delay: float = _BASE_DELAY,
) -> Any:
    """Call ``make_call`` (one model request), retrying transient overloads.

    Exponential backoff (``base_delay * 2**attempt``, capped at 30s) on a
    rate-limit / overload; any other exception propagates immediately, and a
    rate-limit that survives ``max_retries`` re-raises (we tried, and stopping
    is better than spinning forever). ``time.sleep`` is module-level so a test
    can patch it to make the backoff path instant.
    """
    for attempt in range(max_retries + 1):
        try:
            return make_call()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if not _is_rate_limit(exc) or attempt == max_retries:
                raise
            time.sleep(min(base_delay * (2**attempt), 30.0))


@dataclass
class AgentRun:
    """The product of one real-agent run — everything needed to judge the outcome.

    ``transcript`` is the full message list (including the model's tool calls
    and the tool-result blocks fed back to it). Its exact shape is the active
    backend's wire shape — the Anthropic path keeps system separate and uses
    ``tool_result`` content blocks; the OpenAI path carries a leading
    ``system`` message and ``role="tool"`` result messages. ``journal`` is
    ``ctx.txn.effects`` — the Pherix effect journal the run produced. ``audit``
    is the journal's persistent handle (query it by ``txn_id`` / ``client_id``).
    ``final_state`` is the terminal :class:`TxnState`. ``dry_run_result``
    carries the :class:`pherix.DryRunResult` when ``mode="dry_run"``. ``error``
    holds a commit-time refusal — an engine one (gate / isolation / policy) or a
    caller-declared domain one (see ``run_agent``'s ``commit_refusals``) — if
    the transaction could not commit. The run still returns rather than raising,
    so the caller can inspect what happened.

    ``governed`` is ``True`` for the normal (Pherix-in-the-path) run and
    ``False`` for the *ungoverned* "before" run (``run_agent(governed=False)``):
    there is no transaction, so ``txn_id`` / ``final_state`` are ``None``, the
    ``journal`` is empty (the whole point — nothing is journalled), and the
    ``audit`` handle stays empty. The run is judged by querying the **real
    resources** the effects hit, not the journal.
    """

    transcript: list[dict]
    journal: list
    audit: AuditJournal
    txn_id: str | None
    final_state: TxnState | None
    dry_run_result: Any = None
    error: Exception | None = None
    stop_reason: str | None = None
    turns: int = 0
    governed: bool = True


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
    api: str = "anthropic",
    base_url: str | None = None,
    governed: bool = True,
    handles: dict[str, Any] | None = None,
) -> AgentRun:
    """Run a real agent on ``task`` with Pherix wrapping its tool calls.

    ``tools`` is a list of ``@tool``-decorated callables (each carries a
    ``.tool_spec``). ``adapters`` / ``policy`` / ``client_id`` are passed
    straight to ``agent_txn`` / ``dry_run``. ``mode`` is ``"commit"`` (the
    transaction commits on a clean loop exit) or ``"dry_run"`` (it rolls back
    and ``dry_run_result`` is populated).

    ``api`` selects the chat backend: ``"anthropic"`` (the Messages API) or
    ``"openai"`` (any OpenAI-compatible chat-completions endpoint). For the
    OpenAI path, ``base_url`` points at the local server (e.g.
    ``"http://localhost:11434/v1"`` for Ollama, ``"http://localhost:8000/v1"``
    for vLLM); it is read from ``OPENAI_BASE_URL`` when omitted. The two paths
    dispatch through Pherix identically — that is the model-blindness proof.
    ``client`` is a client matching the chosen backend; when ``None`` the real
    SDK is constructed lazily (needs a key / a reachable endpoint). Tests always
    inject a mock, so no SDK import and no network happen offline.

    ``isolation`` (commit mode only) is the resolution policy passed to
    ``agent_txn`` — ``Abort`` / ``Retry`` / ``Serialize`` — for the concurrent
    dogfoods; ``dry_run`` takes no isolation (it never competes to commit).

    ``commit_refusals`` lets a caller declare *domain* exception types that
    should be captured onto ``AgentRun.error`` exactly like the engine's own
    commit-time refusals, instead of propagating. A domain tool that raises at
    commit-time (e.g. a staged smoke-test that fails inside the fire loop) is a
    first-class ``_partial_unwind`` path — capturing it lets the caller inspect
    the unwound ``AgentRun`` rather than wrap the call in try/except.

    ``governed`` (default ``True``) is the *world* the run lives in. The default
    is the normal Pherix-in-the-path run described above. ``governed=False`` is
    the **ungoverned "before"**: the same model loop, the same backend seam, the
    same tools — but **no** ``agent_txn``, so no policy, no journal, no snapshot,
    no gate. Each call fires straight at the real resource and *persists*. This
    needs ``handles`` because the ``@tool`` wrapper, called outside ``agent_txn``,
    is a transparent passthrough that does **not** inject the resource handle
    (see ``pherix/core/tools.py``): the ungoverned path therefore dispatches each
    call itself as ``spec.fn(handles[resource], **args)`` for handle-injecting
    tools (e.g. ``sql`` → the live connection, ``fs`` → an
    :class:`UngovernedFsHandle`) and ``spec.fn(**args)`` otherwise (e.g.
    ``http`` / ``git`` / ``shell``). ``handles`` maps ``resource -> handle`` and
    is required when ``governed=False``. The returned :class:`AgentRun` has no
    transaction (``txn_id`` / ``final_state`` are ``None``, ``journal`` empty) —
    the "before" is judged by querying the real resources, not the journal. The
    governed branch below is untouched, so its behaviour stays byte-identical.
    """
    if mode not in ("commit", "dry_run"):
        raise ValueError(f"mode must be 'commit' or 'dry_run', got {mode!r}")
    if mode == "dry_run" and isolation is not None:
        raise ValueError(
            "isolation has no meaning in dry_run mode (a dry-run never commits, "
            "so it never competes for a conflict)"
        )
    if not governed and handles is None:
        raise ValueError(
            "ungoverned runs (governed=False) need handles={resource: handle} so "
            "the loop can inject the resource handle the @tool wrapper would have "
            "injected inside agent_txn (e.g. {'sql': conn, 'fs': UngovernedFsHandle(root)})"
        )

    backend = _backend_for(api)
    policy = policy or Policy.allow_all()
    audit = audit or AuditJournal.in_memory()
    client = client or backend.default_client(base_url)

    tool_map = {w.tool_spec.name: w for w in tools}
    tool_defs = backend.tool_defs(tools)
    messages: list[dict] = backend.initial_messages(system=system, task=task)

    state: dict[str, Any] = {"stop": None, "turns": 0}

    def _loop(ungoverned_handles: dict[str, Any] | None = None) -> None:
        for _ in range(max_turns):
            state["turns"] += 1
            resp = _create_with_backoff(
                lambda: backend.create(
                    client,
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tool_defs=tool_defs,
                    messages=messages,
                )
            )
            state["stop"] = backend.stop_reason(resp)
            messages.extend(backend.assistant_messages(resp))
            calls = backend.tool_calls(resp)
            if not calls:
                return
            results = [
                _Outcome(
                    call,
                    *_invoke_tool(
                        call.name, call.args, tool_map, handles=ungoverned_handles
                    ),
                )
                for call in calls
            ]
            messages.extend(backend.result_messages(results))

    # The ungoverned "before": no transaction at all. The same model loop runs,
    # but each call fires straight at the real resource via ``handles`` and
    # persists — there is nothing to commit, roll back, or journal. We return an
    # AgentRun with no transaction so the caller judges it by the resource state.
    if not governed:
        _loop(ungoverned_handles=handles)
        return AgentRun(
            transcript=messages,
            journal=[],
            audit=audit,
            txn_id=None,
            final_state=None,
            dry_run_result=None,
            error=None,
            stop_reason=state["stop"],
            turns=state["turns"],
            governed=False,
        )

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


# --- tool dispatch (backend-agnostic — the model-blind core) ---------------


@dataclass
class _ToolCall:
    """One tool invocation a model emitted, normalised across backends."""

    id: Any
    name: str | None
    args: dict


@dataclass
class _Outcome:
    """A dispatched tool call paired with its (content, is_error) result."""

    call: _ToolCall
    content: str
    is_error: bool


def _invoke_tool(
    name: str | None,
    args: dict,
    tool_map: dict[str, Callable[..., Any]],
    handles: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Call one tool; return ``(content, is_error)``.

    This is the single dispatch point both backends funnel through — identical
    regardless of which model API produced the call, which is exactly why a
    local model is governed the same as cloud Claude. A ``PolicyViolation``
    (stage-time deny) becomes an error result the model reads and adapts to —
    the denied call left nothing in the journal. Any other tool exception is
    likewise reported back rather than crashing the loop, so a single bad call
    doesn't abort the whole run.

    ``handles`` distinguishes the two worlds. ``None`` → **governed**: the call
    goes through the ``@tool`` wrapper, which (inside ``agent_txn``) journals,
    snapshots, policy-checks and audits it. A dict → **ungoverned "before"**:
    we bypass the wrapper (outside ``agent_txn`` it would passthrough *without*
    injecting the handle) and call the underlying ``spec.fn`` directly, injecting
    ``handles[resource]`` for handle-taking tools — the effect fires straight at
    the real resource and persists, with no journal and no policy.
    """
    wrapper = tool_map.get(name)
    if wrapper is None:
        return (f"unknown tool {name!r}", True)
    try:
        if handles is None:
            out = wrapper(**args)
        else:
            out = _invoke_ungoverned(wrapper.tool_spec, args, handles)
    except PolicyViolation as exc:
        return (f"DENIED by policy: {exc}", True)
    except Exception as exc:  # noqa: BLE001 - report tool faults to the model
        return (f"tool error: {type(exc).__name__}: {exc}", True)
    return (_render_output(out), False)


def _invoke_ungoverned(spec: Any, args: dict, handles: dict[str, Any]) -> Any:
    """Dispatch one call in the ungoverned world — fire straight at the resource.

    Mirrors what the runtime does on the governed path *except* the transaction:
    a handle-injecting tool gets ``handles[resource]`` as its first argument (the
    live connection / filesystem handle), an injection-free tool (http / git /
    shell) is called with its declared args alone. No policy, no snapshot, no
    journal — the effect happens and stays.
    """
    if spec.injects_handle:
        return spec.fn(handles[spec.resource], **args)
    return spec.fn(**args)


class UngovernedFsHandle:
    """The "before"-world filesystem handle: writes/deletes hit disk immediately.

    The governed :class:`pherix.core.adapters.filesystem.FsHandle` exposes the
    same ``write`` / ``delete`` / ``read`` surface but takes a copy-on-write
    backup of every first touch so the adapter can ``restore`` it on rollback —
    that backup is what makes a filesystem effect reversible. This handle is
    deliberately that *minus* the safety net: it resolves paths under ``root``
    (so a relative ``.env`` lands inside the scratch tree, not on the real
    machine) and then writes or unlinks straight away. There is no backup and
    nothing to restore — which is exactly the point of the ungoverned demo: the
    secret really is gone, the clobbered file really is clobbered.
    """

    def __init__(self, root: Path | str):
        self._root = Path(root).resolve()

    def _resolve(self, rel_path: str) -> Path:
        candidate = Path(rel_path)
        if candidate.is_absolute():
            raise ValueError(f"path {rel_path!r} is outside root {self._root}")
        target = (self._root / candidate).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"path {rel_path!r} is outside root {self._root}")
        return target

    def write(self, rel_path: str, data: bytes) -> None:
        target = self._resolve(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def delete(self, rel_path: str) -> None:
        self._resolve(rel_path).unlink()

    def read(self, rel_path: str) -> bytes:
        return self._resolve(rel_path).read_bytes()


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


# --- the chat backends -----------------------------------------------------
#
# A backend is the *only* place a model API's wire shape leaks in. Each one
# knows how to: seed the message list, build tool definitions in its schema,
# call the model, and read a response back into the normalised (_ToolCall /
# stop_reason / assistant messages / result messages) vocabulary the loop and
# the Pherix dispatch share. Nothing below this line touches an adapter, the
# journal, or a policy — that is the seam that keeps Pherix model-blind.


class _ChatBackend:
    name: str

    def default_client(self, base_url: str | None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def initial_messages(self, *, system: str, task: str) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def tool_defs(self, tools: list[Callable[..., Any]]) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def create(self, client, *, model, max_tokens, system, tool_defs, messages):  # pragma: no cover
        raise NotImplementedError

    def stop_reason(self, resp: Any) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def assistant_messages(self, resp: Any) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def tool_calls(self, resp: Any) -> list[_ToolCall]:  # pragma: no cover
        raise NotImplementedError

    def result_messages(self, results: list[_Outcome]) -> list[dict]:  # pragma: no cover
        raise NotImplementedError


def _backend_for(api: str) -> _ChatBackend:
    if api == "anthropic":
        return _AnthropicBackend()
    if api == "openai":
        return _OpenAIBackend()
    raise ValueError(f"api must be 'anthropic' or 'openai', got {api!r}")


# --- Anthropic backend (Messages API) --------------------------------------


class _AnthropicBackend(_ChatBackend):
    """The Anthropic Messages API shape: system kwarg, ``tool_use`` content
    blocks, ``tool_result`` blocks fed back inside a user message."""

    name = "anthropic"

    def default_client(self, base_url: str | None) -> Any:
        return _default_anthropic_client()

    def initial_messages(self, *, system: str, task: str) -> list[dict]:
        # System is a separate kwarg on this API, so it is not in the message
        # list — preserves the historical transcript shape the tests assert on.
        return [{"role": "user", "content": task}]

    def tool_defs(self, tools: list[Callable[..., Any]]) -> list[dict]:
        return [_anthropic_tool_def(w.tool_spec) for w in tools]

    def create(self, client, *, model, max_tokens, system, tool_defs, messages):
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tool_defs,
            messages=messages,
        )

    def stop_reason(self, resp: Any) -> str | None:
        return getattr(resp, "stop_reason", None)

    def assistant_messages(self, resp: Any) -> list[dict]:
        blocks = list(resp.content)
        return [{"role": "assistant", "content": [_block_to_dict(b) for b in blocks]}]

    def tool_calls(self, resp: Any) -> list[_ToolCall]:
        calls = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                calls.append(
                    _ToolCall(
                        id=getattr(b, "id", None),
                        name=getattr(b, "name", None),
                        args=getattr(b, "input", None) or {},
                    )
                )
        return calls

    def result_messages(self, results: list[_Outcome]) -> list[dict]:
        blocks = [
            _tool_result(o.call.id, o.content, is_error=o.is_error) for o in results
        ]
        return [{"role": "user", "content": blocks}]


# --- OpenAI-compatible backend (chat-completions) --------------------------


class _OpenAIBackend(_ChatBackend):
    """Any OpenAI-compatible chat-completions endpoint (Ollama / vLLM / …).

    The wire shape differs from Anthropic — system is a leading message,
    tools are ``{"type":"function", ...}``, tool calls carry a JSON *string*
    of arguments, and results go back as ``role="tool"`` messages — but every
    one of those differences is confined to this class. The (name, args) the
    loop dispatches and the Pherix machinery behind it are identical.
    """

    name = "openai"

    def default_client(self, base_url: str | None) -> Any:
        return _default_openai_client(base_url)

    def initial_messages(self, *, system: str, task: str) -> list[dict]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]

    def tool_defs(self, tools: list[Callable[..., Any]]) -> list[dict]:
        return [_openai_tool_def(w.tool_spec) for w in tools]

    def create(self, client, *, model, max_tokens, system, tool_defs, messages):
        # System already lives in ``messages`` for this API, so it is not
        # passed separately. ``tools`` is omitted when empty — some servers
        # reject an empty tools array.
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if tool_defs:
            kwargs["tools"] = tool_defs
        return client.chat.completions.create(**kwargs)

    def stop_reason(self, resp: Any) -> str | None:
        choice = resp.choices[0]
        return getattr(choice, "finish_reason", None)

    def assistant_messages(self, resp: Any) -> list[dict]:
        msg = resp.choices[0].message
        out: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(msg, "content", None),
        }
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            out["tool_calls"] = [
                {
                    "id": getattr(tc, "id", None),
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        return [out]

    def tool_calls(self, resp: Any) -> list[_ToolCall]:
        msg = resp.choices[0].message
        calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            calls.append(
                _ToolCall(
                    id=getattr(tc, "id", None),
                    name=tc.function.name,
                    args=_parse_json_args(tc.function.arguments),
                )
            )
        return calls

    def result_messages(self, results: list[_Outcome]) -> list[dict]:
        # No is_error flag in this API — the refusal text ("DENIED ...",
        # "tool error: ...") is the model-readable signal, exactly as on the
        # Anthropic path's error block.
        return [
            {
                "role": "tool",
                "tool_call_id": o.call.id,
                "content": o.content,
            }
            for o in results
        ]


def _parse_json_args(raw: Any) -> dict:
    """Decode a tool call's JSON-string arguments to a dict.

    OpenAI-compatible servers send tool-call arguments as a JSON *string*.
    A malformed / empty payload degrades to ``{}`` so a single bad call is
    reported to the model (unknown args -> tool error) rather than crashing.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- tool-schema plumbing --------------------------------------------------


_TYPE_MAP = {
    int: "integer",
    float: "number",
    bool: "boolean",
    str: "string",
    bytes: "string",
}


def _tool_schema(spec: Any) -> tuple[dict, str]:
    """Build the JSON-schema ``input`` object + a description for a ``ToolSpec``.

    Shared by both backends: the agent sees the *public* signature (the injected
    adapter handle removed), param types mapped from annotations (defaulting to
    string), params without a default marked ``required``.
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
    return schema, _description(spec)


def _anthropic_tool_def(spec: Any) -> dict:
    """Anthropic tool definition (``input_schema``) from a Pherix ``ToolSpec``."""
    schema, description = _tool_schema(spec)
    return {
        "name": spec.name,
        "description": description,
        "input_schema": schema,
    }


def _openai_tool_def(spec: Any) -> dict:
    """OpenAI-compatible function-tool definition from a Pherix ``ToolSpec``.

    Same JSON-schema for the parameters as the Anthropic def — only the
    envelope differs (``{"type":"function","function":{...,"parameters":...}}``).
    """
    schema, description = _tool_schema(spec)
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": description,
            "parameters": schema,
        },
    }


def _description(spec: Any) -> str:
    import inspect

    doc = inspect.getdoc(spec.fn)
    if doc:
        return doc.strip().splitlines()[0]
    return f"Pherix tool {spec.name!r} on resource {spec.resource!r}."


def _block_to_dict(block: Any) -> dict:
    """Normalise an Anthropic response content block to a plain dict."""
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


def _tool_result(tool_use_id: Any, content: str, *, is_error: bool = False) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


# --- real-run plumbing (never reached on the offline test path) ------------


def _default_anthropic_client() -> Any:
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


def _default_openai_client(base_url: str | None) -> Any:
    """Construct an OpenAI-compatible client pointed at a local endpoint.

    ``base_url`` (or ``OPENAI_BASE_URL``) names the local server — e.g.
    ``http://localhost:11434/v1`` (Ollama) or ``http://localhost:8000/v1``
    (vLLM). A local server typically ignores the key, so ``OPENAI_API_KEY``
    falls back to a placeholder; a remote OpenAI-compatible endpoint that *does*
    require a key should have it set. Lazy import keeps the ``openai`` SDK out
    of the offline suite — tests inject a mock instead.
    """
    _load_env()
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "No base_url for the OpenAI-compatible endpoint. Pass "
            "base_url='http://localhost:11434/v1' (Ollama) / "
            "'http://localhost:8000/v1' (vLLM), or set OPENAI_BASE_URL. Offline "
            "tests must pass a mock `client=` instead."
        )
    key = os.environ.get("OPENAI_API_KEY", "not-needed-for-local")
    import openai  # examples-only dependency; pip install -e '.[dogfood]'

    return openai.OpenAI(base_url=base_url, api_key=key)


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
