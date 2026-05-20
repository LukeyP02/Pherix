"""The agent-agnostic coding sandbox — environment-level interception.

A coding CLI (Claude Code, Cursor, Gemini CLI, an open-source agent like
Goose/Cline) does its real work through *built-in* tools: it edits files, it
runs ``git``, it runs shell. MCP can *add* tools to such a CLI but it cannot
*intercept* those built-ins — and a Claude-Code hook only governs Claude Code.

So we don't build a custom coding agent (that would defeat the point — an
enterprise runs an out-of-box CLI). We build a **sandbox**: an environment the
out-of-box CLI runs *inside*, where its filesystem and shell calls are
transparently routed through Pherix. Two interception surfaces, one idea:

  * **Filesystem** — a Pherix copy-on-write overlay (:class:`FilesystemAdapter`)
    rooted at a throwaway repo. Every file write/delete the CLI makes is a
    journalled, snapshotted, policy-gated, audited :class:`Effect`.
  * **PATH** — shimmed ``git`` and ``sh`` binaries placed *first* on the
    sandbox's ``PATH``. When the CLI shells out to ``git push`` or ``rm -rf``,
    the OS resolves *our* shim, which forwards the argv back into this same
    Pherix transaction instead of the real binary.

"Build once, govern everything." The sandbox is agent-agnostic: it intercepts
at the environment level (the filesystem root + ``PATH``), so it works for any
CLI that edits files and shells out — which is all of them. That neutrality is
the moat; a Claude-Code-only hook does not have it.

This module is the **routing layer** — the part the shims call and the part the
offline test drives directly. It needs no LLM and no key: it is pure mechanism.
A real CLI run wires this up via :func:`sandbox_env` (PATH + a session pointer)
and is exercised by ``__main__``; the automated proof is
``tests/test_dogfood_coding.py``, which simulates a CLI's built-in tool calls
hitting :meth:`Sandbox.route` and asserts the gate + audit fire.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from pherix.core.audit import AuditJournal
from pherix.core.effects import StagedResult
from pherix.core.policy import Allow, Cap, Deny, Policy, PolicyViolation
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import REGISTRY, tool


# --- the CLI's built-in actions, as routed tool calls ----------------------
#
# A coding CLI emits three kinds of side-effecting built-in action. The shims
# normalise each into one of these route "verbs"; :meth:`Sandbox.route`
# dispatches a verb to the matching Pherix ``@tool``. The agent never sees
# these names — it just edits files and runs commands as usual.

VERB_WRITE = "write_file"   # CLI's Edit/Write built-in   -> reversible FS effect
VERB_DELETE = "delete_file"  # CLI's delete                -> reversible FS effect
VERB_GIT = "git"            # CLI shells out to `git ...`  -> irreversible effect
VERB_SHELL = "shell"        # CLI shells out to `sh -c ..` -> irreversible effect


@dataclass
class RouteOutcome:
    """The result of routing one CLI built-in action through Pherix.

    ``allowed`` is False when the action was **gated** by policy (denied at
    stage-time, or blocked at the commit gate) — the shim renders this as a
    non-zero exit so the CLI sees a refusal and adapts, exactly as it would if
    the real binary had failed. ``journalled`` is True when an :class:`Effect`
    landed in the transaction (a denied action journals nothing). ``staged``
    marks an irreversible action recorded as intent (it fires at commit).
    """

    verb: str
    allowed: bool
    journalled: bool
    detail: str
    staged: bool = False
    effect_id: str | None = None


# --- the policy the sandbox runs under -------------------------------------


def _path_arg(args: dict) -> str:
    return str(args.get("path", ""))


def _is_protected_path(path: str) -> bool:
    """A path the CLI must never touch — secrets and system files.

    The FilesystemAdapter already confines writes to the repo root (``..`` and
    absolute escapes are rejected), so ``/etc`` is unreachable *structurally*.
    This rule is the **policy-level** statement of the same intent, expressed
    on the journalled effect's args so it shows up as a clean ``Deny`` (a
    GATED effect in the audit) rather than a raw adapter error — and so it also
    catches in-root secrets like ``.env`` / ``secrets/**`` that the root
    confinement would otherwise allow.
    """
    p = path.replace("\\", "/").lstrip("/")
    if path.startswith("/etc") or path.startswith("/") and "etc/" in path:
        return True
    parts = p.split("/")
    if ".env" in parts or "secrets" in parts:
        return True
    if p.endswith(".env") or "id_rsa" in p or p.endswith(".pem"):
        return True
    return False


def edits_confined_to_src(effect: Any, ctx: Any) -> Allow | Deny:
    """Rule: file writes/deletes may only land under ``src/**``.

    Expressed over ``effect.args['path']`` so a write to ``/etc/passwd`` or a
    secret denies with a readable reason and the effect is recorded GATED. A
    write under ``src/`` (or the always-allowed repo metadata the CLI needs)
    passes.
    """
    if effect.tool not in ("sandbox_write", "sandbox_delete"):
        return Allow()
    path = _path_arg(effect.args)
    if _is_protected_path(path):
        return Deny(f"path {path!r} is a protected/secret location — edits forbidden")
    norm = path.replace("\\", "/").lstrip("/")
    if not norm.startswith("src/"):
        return Deny(f"edits are confined to src/**; {path!r} is outside it")
    return Allow()


def no_push_to_main(effect: Any, ctx: Any) -> Allow | Deny:
    """Rule: the CLI may ``git commit`` freely but must not ``git push`` to main.

    The CLI's ``git`` shim normalises the argv into ``effect.args['argv']`` (a
    list). Commit is fine; a push targeting ``main`` / ``master`` denies. This
    is the canonical "can change local state, cannot publish it" boundary.
    """
    if effect.tool != "sandbox_git":
        return Allow()
    argv = effect.args.get("argv") or []
    argv = [str(a) for a in argv]
    if argv and argv[0] == "push":
        rest = " ".join(argv[1:])
        if "main" in argv[1:] or "master" in argv[1:] or rest == "":
            return Deny("`git push` to main/master is forbidden in the sandbox")
    return Allow()


def coding_policy() -> Policy:
    """The sandbox's governing policy.

    Three boundaries, each a fold over the journalled effect:
      * ``edits_confined_to_src`` — may edit ``src/**``, not ``/etc`` or secrets.
      * ``no_push_to_main`` — may commit, must not push to main.
      * a **spend-cap** on shell calls — at most 3 ``sandbox_shell`` effects per
        session, so a runaway agent cannot hammer the shell.
    """
    return Policy.with_rules(
        rules=[edits_confined_to_src, no_push_to_main],
        caps=[Cap.count(tool="sandbox_shell", max=3)],
    )


# --- the sandbox -----------------------------------------------------------


@dataclass
class Sandbox:
    """A Pherix-governed environment a coding CLI runs inside.

    Construct one over a repo root (a :func:`infra.scratch_repo` for a real
    run; any directory for the offline test), then either:

      * drive it directly via :meth:`route` — what the offline test and the
        shims do, simulating the CLI's built-in tool calls; or
      * enter :meth:`session` and export :func:`sandbox_env` so a real
        out-of-box CLI shells out into the shims.

    The transaction spans the whole session: reversible FS edits apply live and
    roll back on a failed/aborted session; irreversible git/shell actions stage
    and fire (or gate) at commit. The audit journal carries ``client_id`` so
    every action is attributable to the agent that drove the run.
    """

    root: Path
    client_id: str
    audit: AuditJournal = field(default_factory=AuditJournal.in_memory)
    policy: Policy = field(default_factory=coding_policy)
    _ctx: Any = field(default=None, init=False, repr=False)
    _last_txn_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    def audit_txn_id(self) -> str:
        """The txn_id of the most recent (or current) session — for audit reads."""
        if self._ctx is not None:
            return self._ctx.txn_id
        if self._last_txn_id is None:
            raise RuntimeError("no session has run yet")
        return self._last_txn_id

    # --- routing: one CLI built-in action -> one Pherix effect -------------

    def route(self, verb: str, **payload: Any) -> RouteOutcome:
        """Route one CLI built-in action through the active transaction.

        This is the single interception entry point the shims and the offline
        test both call. ``verb`` is one of the ``VERB_*`` constants; ``payload``
        carries the action's data (``path`` + ``content`` for a write, ``argv``
        for git/shell). A :class:`PolicyViolation` (stage-time deny) is caught
        and surfaced as an *un-allowed* outcome — the shim turns that into a
        non-zero exit so the CLI adapts, and nothing was journalled. A real tool
        fault is surfaced the same honest way.

        Must be called inside :meth:`session` (or with an externally-opened
        transaction); the bound ``@tool`` wrappers route to the active txn.
        """
        if self._ctx is None:
            raise RuntimeError(
                "Sandbox.route called outside a session; use `with sandbox.session():`"
            )
        dispatch = {
            VERB_WRITE: self._do_write,
            VERB_DELETE: self._do_delete,
            VERB_GIT: self._do_git,
            VERB_SHELL: self._do_shell,
        }
        fn = dispatch.get(verb)
        if fn is None:
            return RouteOutcome(verb, allowed=False, journalled=False,
                                detail=f"unknown sandbox verb {verb!r}")
        try:
            out = fn(**payload)
        except PolicyViolation as exc:
            # Stage-time gate: nothing journalled, the CLI sees a refusal.
            return RouteOutcome(verb, allowed=False, journalled=False,
                                detail=f"GATED by policy: {exc.reason}")
        except Exception as exc:  # noqa: BLE001 - report faults to the CLI
            return RouteOutcome(verb, allowed=False, journalled=False,
                                detail=f"error: {type(exc).__name__}: {exc}")
        return out

    # --- the session: opens the transaction the shims route into -----------

    @contextmanager
    def session(self) -> Iterator["Sandbox"]:
        """Open the Pherix transaction the CLI's actions are journalled into.

        On a clean exit the transaction commits — at which point staged
        irreversibles (git/shell) fire through their compensators or gate. On
        an exception the whole session rolls back: every reversible FS edit the
        CLI made is restored, nothing irreversible ever happened. The
        ``@tool``-wrapped sandbox operations are registered here (never at
        module top level — the registry is process-global) and torn down after.
        """
        self._register_tools()
        adapters = {"fs": _FsFor(self.root), "shell": _ShellEcho(), "git": _GitEcho()}
        try:
            with agent_txn(
                adapters, policy=self.policy, audit=self.audit,
                client_id=self.client_id,
            ) as ctx:
                self._ctx = ctx
                self._last_txn_id = ctx.txn_id
                yield self
        finally:
            self._ctx = None
            _unregister(_SANDBOX_TOOL_NAMES)

    # --- per-verb handlers (call the @tool wrappers) -----------------------

    def _do_write(self, *, path: str, content: str) -> RouteOutcome:
        sandbox_write(path, content)
        eff = self._ctx.txn.effects[-1]
        return RouteOutcome(VERB_WRITE, allowed=True, journalled=True,
                            detail=f"wrote {path}", effect_id=eff.effect_id)

    def _do_delete(self, *, path: str) -> RouteOutcome:
        sandbox_delete(path)
        eff = self._ctx.txn.effects[-1]
        return RouteOutcome(VERB_DELETE, allowed=True, journalled=True,
                            detail=f"deleted {path}", effect_id=eff.effect_id)

    def _do_git(self, *, argv: list[str]) -> RouteOutcome:
        res = sandbox_git(list(argv))
        eff = self._ctx.txn.effects[-1]
        staged = isinstance(res, StagedResult)
        return RouteOutcome(VERB_GIT, allowed=True, journalled=True, staged=staged,
                            detail=f"git {' '.join(argv)}", effect_id=eff.effect_id)

    def _do_shell(self, *, argv: list[str]) -> RouteOutcome:
        res = sandbox_shell(list(argv))
        eff = self._ctx.txn.effects[-1]
        staged = isinstance(res, StagedResult)
        return RouteOutcome(VERB_SHELL, allowed=True, journalled=True, staged=staged,
                            detail=f"sh {' '.join(argv)}", effect_id=eff.effect_id)

    # --- tool registration (inside session — registry is global) -----------

    def _register_tools(self) -> None:
        _register_sandbox_tools()


# --- the @tool definitions (registered per-session, never at module top) ----
#
# These are module-level *functions* but they are only put into the global
# REGISTRY by ``_register_sandbox_tools`` inside a session, and removed on exit
# — so concurrent sessions / the autouse test fixture never collide.

def _sandbox_write(handle: Any, path: str, content: str) -> str:
    """Write a file (the CLI's Edit/Write built-in) — reversible FS effect."""
    handle.write(path, content.encode("utf-8"))
    return f"wrote {len(content)} bytes to {path}"


def _sandbox_delete(handle: Any, path: str) -> str:
    """Delete a file (the CLI's delete built-in) — reversible FS effect."""
    handle.delete(path)
    return f"deleted {path}"


def _sandbox_git(argv: list[str]) -> str:
    """Run a git command (the CLI shelled out to `git`) — irreversible."""
    return f"git {' '.join(str(a) for a in argv)} (ok)"


def _sandbox_shell(argv: list[str]) -> str:
    """Run a shell command (the CLI shelled out to `sh -c`) — irreversible."""
    return f"sh {' '.join(str(a) for a in argv)} (ok)"


# Bound wrapper handles, populated by _register_sandbox_tools. The module-level
# names the handlers above reference (sandbox_write, ...) are these wrappers.
sandbox_write: Any = None
sandbox_delete: Any = None
sandbox_git: Any = None
sandbox_shell: Any = None

_SANDBOX_TOOL_NAMES = ("sandbox_write", "sandbox_delete", "sandbox_git", "sandbox_shell")


def _register_sandbox_tools() -> None:
    """Register the four sandbox @tools into the global REGISTRY.

    Called inside :meth:`Sandbox.session`. FS ops are reversible (the
    FilesystemAdapter snapshots + restores); git/shell are irreversible
    (``reversible=False``) so they stage and fire at commit — honest about the
    fact that a real ``git push`` or ``rm`` cannot be silently undone. Idempotent
    against a partially-registered state (re-register after a clean teardown).
    """
    global sandbox_write, sandbox_delete, sandbox_git, sandbox_shell
    _unregister(_SANDBOX_TOOL_NAMES)
    sandbox_write = tool("fs", reversible=True, name="sandbox_write")(_sandbox_write)
    sandbox_delete = tool("fs", reversible=True, name="sandbox_delete")(_sandbox_delete)
    sandbox_git = tool(
        "git", reversible=False, name="sandbox_git", injects_handle=False
    )(_sandbox_git)
    sandbox_shell = tool(
        "shell", reversible=False, name="sandbox_shell", injects_handle=False
    )(_sandbox_shell)


def _unregister(names: tuple[str, ...]) -> None:
    for n in names:
        REGISTRY._tools.pop(n, None)


# --- adapters --------------------------------------------------------------
#
# The FS overlay is the real Pherix CoW adapter (no core change). git/shell are
# irreversible "echo" adapters: they record the action as intent and, at commit,
# would fire the real binary. For the PoC they echo (the offline test asserts
# the *journalling + gate*, not real git output); the manual __main__ run wires
# real binaries behind the same shims. supports_rollback() is False, so the
# runtime routes them down the stage-and-gate lane — the correct, honest lane
# for an unundoable action.

def _FsFor(root: Path) -> Any:
    from pherix.core.adapters.filesystem import FilesystemAdapter

    return FilesystemAdapter(root)


class _IrreversibleEcho:
    """Minimal irreversible adapter — stages, fires at commit by calling the fn.

    ``supports_rollback() -> False`` forces the runtime's irreversible lane:
    the effect stages, the agent gets a :class:`StagedResult`, and the fire
    happens at commit. ``apply`` simply runs the tool fn (which, in a real run,
    invokes the genuine binary the shim wraps).
    """

    def __init__(self, name: str):
        self.name = name

    def supports_rollback(self) -> bool:
        return False

    def snapshot(self, effect: Any) -> Any:  # pragma: no cover - never called
        raise RuntimeError(f"{self.name} is irreversible; it has no snapshot")

    def apply(self, effect: Any, tool_fn: Any) -> Any:
        return tool_fn(**effect.args)

    def restore(self, handle: Any) -> None:  # pragma: no cover - never called
        raise RuntimeError(f"{self.name} is irreversible; it cannot restore")


class _ShellEcho(_IrreversibleEcho):
    def __init__(self) -> None:
        super().__init__("shell")


class _GitEcho(_IrreversibleEcho):
    def __init__(self) -> None:
        super().__init__("git")


# --- PATH shims for a real CLI run -----------------------------------------
#
# For the offline test we call Sandbox.route directly. For a real out-of-box CLI
# run, the CLI shells out to `git`/`sh` by name — so we plant shim executables
# *first* on PATH that forward argv into a running sandbox session. The session
# advertises itself through an env var pointing at a tiny JSON pointer file; the
# shim reads it and POSTs the action to the session's local dispatch.

_SESSION_ENV = "PHERIX_SANDBOX_SESSION"


def write_shims(bin_dir: Path) -> dict[str, Path]:
    """Write ``git`` and ``sh`` shim executables into ``bin_dir``.

    Each shim is a tiny script that re-invokes this module's ``route_cli``
    entry point with its argv, so when the out-of-box CLI runs ``git push`` the
    OS resolves *our* git and the call lands in Pherix. Returns the shim paths.
    Real executables (mode 0o755) so they satisfy the CLI's PATH lookup; the
    offline test does not need them (it calls :meth:`Sandbox.route`).
    """
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    shims: dict[str, Path] = {}
    py = "python3"
    for binary, verb in (("git", VERB_GIT), ("sh", VERB_SHELL)):
        path = bin_dir / binary
        script = (
            "#!/usr/bin/env bash\n"
            f'exec {py} -m examples.dogfood.coding route-cli {verb} "$@"\n'
        )
        path.write_text(script)
        path.chmod(0o755)
        shims[binary] = path
    return shims


@contextmanager
def sandbox_env(sandbox: Sandbox, bin_dir: Path) -> Iterator[dict[str, str]]:
    """Yield the environment a real CLI should run under to be governed.

    Plants the shims on a copy of ``PATH`` (ours first) and points
    ``PHERIX_SANDBOX_SESSION`` at a pointer file describing the live session, so
    a shim invocation can find and route into it. The CLI is launched with this
    env (``subprocess.run(cli_argv, env=env, cwd=sandbox.root)``). The pointer
    carries the audit DB path + client_id so the shim re-attaches a route to the
    same audited transaction. (The full cross-process re-attach is the
    not-yet-built piece — see this module's README "Honest limits".)
    """
    shims_dir = Path(bin_dir)
    write_shims(shims_dir)
    pointer = shims_dir / "session.json"
    pointer.write_text(json.dumps({
        "root": str(sandbox.root),
        "client_id": sandbox.client_id,
    }))
    env = dict(os.environ)
    env["PATH"] = f"{shims_dir}{os.pathsep}{env.get('PATH', '')}"
    env[_SESSION_ENV] = str(pointer)
    yield env
