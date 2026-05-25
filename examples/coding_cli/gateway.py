"""``build_gateway()`` for a coding CLI fronted by Pherix over MCP.

Run it as the MCP server the CLI spawns::

    PHERIX_CODING_REPO=/path/to/repo \
        python -m pherix.frontends.proxy examples.coding_cli.gateway

and register that command with the CLI (see ``FINDINGS.md`` for per-CLI config).
Every tool the agent calls *through MCP* then runs inside a Pherix transaction:
snapshotted, policy-checked, gated, audited — with the session ``client_id`` set
to the CLI's handshake identity.

The governance is the four-axes story made concrete for a coding agent:

  * **Reversible work runs live and rolls back.** ``read_file`` / ``write_file``
    / ``delete_file`` go through a :class:`FilesystemAdapter` copy-on-write
    overlay; local git (``git_command`` — add / commit / ``reset --hard`` /
    checkout) goes through a :class:`GitAdapter`. Each is a snapshotted effect:
    if the transaction unwinds, the file tree and ``HEAD`` are restored exactly.
    ``apply_code_edit`` adds a post-write compile check — broken Python is
    written, rejected, and *reverted by Pherix*, never left on disk.
  * **Irreversible work stages and gates.** ``git_push`` and ``run_shell`` route
    through :class:`ProcessAdapter` (``supports_rollback() -> False``), so they
    do not fire at stage-time; they are recorded as intent and *gate* at commit
    (every staged irreversible needs a compensator or out-of-band approval, and
    a stateless one-shot MCP call grants neither — so the push never leaves the
    machine and the shell never runs). Pherix is honest that these cannot be
    silently undone.
  * **The obviously dangerous is denied outright.** Force-push (history loss),
    ``rm -rf`` of a protected path, a write/commit of a secret
    (``.env`` / ``*.pem`` / ``id_rsa`` / ``secrets/**``), and pushing via the
    reversible ``git_command`` lane are all *denied at stage-time* — a stronger
    refusal than the gate: nothing is journalled APPLIED, no resource is touched,
    and the agent reads the refusal and adapts.
  * a **count cap** keeps a runaway agent from spamming shell calls.

Importing this module registers the ``@tool`` functions into the global
registry, so ``tools/list`` enumerates exactly these. The repo root, audit
journal, and granted identities are wired by :func:`build_gateway`; the SQLite
audit defaults to in-memory so the module is runnable and offline-testable as-is.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.git import GitAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect
from pherix.core.policy import Allow, Cap, Deny, Policy
from pherix.core.tools import tool
from pherix.frontends.proxy import PherixGateway

from examples.coding_cli import CODING_CLI_IDENTITIES

# --------------------------------------------------------------------------
# Secret + destructive-path predicates — the shared vocabulary the policy
# rules fold over. Kept as module-level helpers so a buyer can read exactly
# what "a secret" and "a protected path" mean and widen them for their repo.
# --------------------------------------------------------------------------

# Glob patterns matched against a write/delete target's relative path. These
# are the credentials a coding agent must never write or commit.
_SECRET_GLOBS = (
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "secrets/*",
    "secrets/**",
    "*/secrets/*",
    "**/credentials",
    "**/credentials.*",
)

# Targets that must never be the operand of a recursive force-remove. A
# ``rm -rf`` of any of these is repo-destroying or worse.
_PROTECTED_RM_TARGETS = (".", "/", "~", "*", "..", ".git", "./", "/*")


def _is_secret_path(path: str) -> bool:
    """True if ``path`` names a credential the agent must not touch."""
    norm = path.lstrip("./")
    return any(
        fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(norm, pat)
        for pat in _SECRET_GLOBS
    )


def _is_push_command(command: str) -> bool:
    """True if a raw git command string is a push (must use ``git_push``)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    # Drop a leading literal "git" if the model wrote "git push ...".
    if argv and argv[0] == "git":
        argv = argv[1:]
    return bool(argv) and argv[0] == "push"


def _is_force_push(remote: str, branch: str, force: bool, extra: str) -> bool:
    """True if a ``git_push`` call would rewrite remote history."""
    if force:
        return True
    blob = f"{remote} {branch} {extra}".lower()
    # `--force`, `-f`, and the `+refspec` force-push spelling all lose history.
    return "--force" in blob or " -f" in f" {blob}" or "+" in branch


def _is_destructive_rm(command: str) -> bool:
    """True if a shell command is a recursive force-remove of a protected path."""
    try:
        argv = shlex.split(command)
    except ValueError:
        # Unparseable shell is itself suspicious — treat as destructive.
        return True
    if not argv or os.path.basename(argv[0]) != "rm":
        return False
    flags = "".join(a for a in argv[1:] if a.startswith("-"))
    recursive_force = ("r" in flags and "f" in flags)
    if not recursive_force:
        return False
    operands = [a for a in argv[1:] if not a.startswith("-")]
    # rm -rf with no operand, or any protected operand, is denied.
    if not operands:
        return True
    return any(op in _PROTECTED_RM_TARGETS or op.startswith("/") for op in operands)


# --------------------------------------------------------------------------
# ProcessAdapter — the irreversible lane for push + shell.
#
# A push leaves the machine and a shell command's side-effects are not
# snapshot-restorable, so this adapter reports ``supports_rollback() -> False``:
# the runtime stages these effects and fires (or gates) them at commit, never
# live. It injects a root-aware :class:`ProcessHandle` exactly as the FS / git
# adapters inject their handles, so the tool body stays a thin wrapper. It lives
# here (an example), not in ``pherix/core`` — the kernel's canonical irreversible
# adapter is HTTPAdapter; this is the same shape specialised to a subprocess.
# --------------------------------------------------------------------------


class ProcessHandle:
    """Per-effect handle for irreversible subprocess tools, rooted at the repo."""

    def __init__(self, root: Path):
        self._root = root

    def run(self, argv: list[str]) -> str:
        """Run ``argv`` rooted at the repo; return stdout (raise on failure)."""
        proc = subprocess.run(
            argv, cwd=str(self._root), capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()


class ProcessAdapter:
    """Irreversible ``ResourceAdapter`` for push / shell (stages, never undoes).

    Conforms to :class:`ResourceAdapter` only — like HTTPAdapter, a subprocess
    has no transaction-scope lifecycle Pherix can drive. ``supports_rollback()``
    is ``False``, which is the runtime's signal to stage the effect and defer
    its fire to commit-time (where it gates without a compensator/approval).
    """

    name = "proc"

    def __init__(self, root: Path | str):
        self._root = Path(root).resolve()

    def supports_rollback(self) -> bool:
        return False

    def snapshot(self, effect: Effect) -> SnapshotHandle:  # pragma: no cover
        raise RuntimeError(
            "ProcessAdapter.snapshot() must not be called: irreversible effects "
            "are staged at stage-time and fired at commit-time."
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # Inject the root-aware handle as the tool's first arg (the @tool
        # wrapper hides it from the agent's call-site, same as FS / git).
        return tool_fn(ProcessHandle(self._root), **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:  # pragma: no cover
        raise RuntimeError(
            "ProcessAdapter.restore() must not be called: there is no "
            "before-state to restore for a push / shell side-effect."
        )


# --------------------------------------------------------------------------
# The governed tools. fs + local-git are reversible (snapshot/restore);
# push + shell are irreversible (stage/gate).
# --------------------------------------------------------------------------


@tool(resource="fs")
def read_file(handle, path):
    """Read a repo file as UTF-8 text (reversible — a read changes nothing)."""
    return handle.read(path).decode("utf-8")


@tool(resource="fs")
def write_file(handle, path, content):
    """Write UTF-8 text to a repo file (reversible — rolled back on unwind)."""
    handle.write(path, content.encode("utf-8"))
    return f"wrote {len(content)} bytes to {path!r}"


@tool(resource="fs")
def delete_file(handle, path):
    """Delete a repo file (reversible — the prior contents are restored on unwind)."""
    handle.delete(path)
    return f"deleted {path!r}"


@tool(resource="fs")
def apply_code_edit(handle, path, content):
    """Write code, then reject it if a ``.py`` file no longer parses.

    The write lands live; the compile check runs *after* it. On a SyntaxError
    the tool raises, and Pherix's reversible lane restores the file to its
    pre-edit bytes — broken code is never left on disk. The relatable enterprise
    fear ("the agent will break my build") answered by snapshot/restore, not by
    trusting the agent.
    """
    handle.write(path, content.encode("utf-8"))
    if path.endswith(".py"):
        # SyntaxError propagates → the effect FAILS → agent_txn rolls the
        # live write back via FilesystemAdapter.restore.
        compile(content, path, "exec")
    return f"applied edit to {path!r} ({len(content)} bytes)"


@tool(resource="git")
def git_command(handle, command):
    """Run one *local* git command (add / commit / reset / checkout / branch).

    Reversible: the GitAdapter snapshots HEAD + dirty + untracked state before
    the command and restores it on unwind — even a ``reset --hard`` that
    destroyed commits comes back via the reflog. Pushing is *not* allowed here
    (the policy denies it); a push is irreversible and must use ``git_push``.
    """
    return handle.run(command)


@tool(resource="proc", reversible=False, injects_handle=True)
def git_push(handle, remote="origin", branch="HEAD", force=False, extra=""):
    """Push to a remote (irreversible — stages and gates at commit).

    A push leaves the machine and cannot be cleanly un-pushed, so it never fires
    live. Without a compensator or out-of-band approval the commit gate blocks
    it. A *force* push (history loss) is denied outright by policy.
    """
    argv = ["git", "push"]
    if force:
        argv.append("--force")
    if extra:
        argv.extend(shlex.split(extra))
    argv.extend([remote, branch])
    return handle.run(argv)


@tool(resource="proc", reversible=False, injects_handle=True)
def run_shell(handle, command):
    """Run an arbitrary shell command (irreversible — stages and gates at commit).

    Shell side-effects are not snapshot-restorable, so a shell call gates rather
    than fires within a one-shot governed call. A ``rm -rf`` of a protected path
    is denied outright by policy before it can even stage.
    """
    return handle.run(shlex.split(command))


# --------------------------------------------------------------------------
# The policy — each rule a fold over a journalled effect's args.
# --------------------------------------------------------------------------


def no_secret_writes(effect, ctx) -> Allow | Deny:
    """Deny writing or deleting a credential file (``.env`` / ``*.pem`` / …)."""
    if effect.tool in ("write_file", "delete_file", "apply_code_edit"):
        path = effect.args.get("path", "")
        if _is_secret_path(path):
            return Deny(f"writing secret path {path!r} is forbidden")
    return Allow()


def no_secret_in_git(effect, ctx) -> Allow | Deny:
    """Deny a local git command that stages/commits a secret path."""
    if effect.tool == "git_command":
        command = effect.args.get("command", "")
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        if any(_is_secret_path(tok) for tok in argv):
            return Deny(f"git command {command!r} references a secret path")
    return Allow()


def no_push_via_git_command(effect, ctx) -> Allow | Deny:
    """Force pushes through the gated ``git_push`` tool, not the reversible lane.

    ``git_command`` runs *live* (it is reversible); letting a ``push`` through it
    would fire an irreversible network effect at stage-time, bypassing the gate.
    """
    if effect.tool == "git_command" and _is_push_command(
        effect.args.get("command", "")
    ):
        return Deny("push must use the git_push tool (the gated irreversible lane)")
    return Allow()


def no_force_push(effect, ctx) -> Allow | Deny:
    """Deny a force push — rewriting published history is unrecoverable."""
    if effect.tool == "git_push":
        a = effect.args
        if _is_force_push(
            a.get("remote", ""), a.get("branch", ""), a.get("force", False),
            a.get("extra", ""),
        ):
            return Deny("force push rewrites remote history and is forbidden")
    return Allow()


def no_destructive_shell(effect, ctx) -> Allow | Deny:
    """Deny a recursive force-remove of a protected path (``rm -rf /`` etc.)."""
    if effect.tool == "run_shell" and _is_destructive_rm(
        effect.args.get("command", "")
    ):
        return Deny("rm -rf of a protected path is forbidden")
    return Allow()


def coding_cli_policy() -> Policy:
    """The policy a granted coding-CLI session runs under.

    Allows ordinary reversible work (file edits, local git) and lets irreversible
    work (push / shell) *stage* — where the commit gate stops it pending
    approval. The five rules deny the unrecoverable outright; the cap stops a
    runaway shell loop.
    """
    return Policy.with_rules(
        rules=[
            no_secret_writes,
            no_secret_in_git,
            no_push_via_git_command,
            no_force_push,
            no_destructive_shell,
        ],
        caps=[Cap.count(tool="run_shell", max=8)],
    )


def build_gateway(repo_root: Path | str | None = None) -> PherixGateway:
    """Construct the gateway a coding CLI's MCP client connects to.

    ``repo_root`` defaults to ``$PHERIX_CODING_REPO`` then the cwd, so the
    zero-argument form ``build_gateway()`` satisfies the ``python -m
    pherix.frontends.proxy`` factory contract while tests pass a scratch repo
    directly. The three adapters all root at the same repo: file edits and local
    git are reversible; push / shell are the irreversible lane. Every granted
    identity (Aider, Claude Code, the reference client) maps to
    :func:`coding_cli_policy`; any other identity hits the deny-all floor.
    """
    root = Path(
        repo_root
        if repo_root is not None
        else os.environ.get("PHERIX_CODING_REPO", ".")
    ).resolve()

    audit_path = os.environ.get("PHERIX_CODING_AUDIT")
    audit = AuditJournal(audit_path) if audit_path else AuditJournal.in_memory()

    policy = coding_cli_policy()
    return PherixGateway(
        adapters={
            "fs": FilesystemAdapter(root),
            "git": GitAdapter(root),
            "proc": ProcessAdapter(root),
        },
        policies={identity: policy for identity in CODING_CLI_IDENTITIES},
        # Deny-all floor: an unrecognised MCP client never runs unpoliced.
        default_policy=Policy(allow=set()),
        audit=audit,
    )
