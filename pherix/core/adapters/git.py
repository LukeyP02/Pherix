"""GitAdapter — snapshot/restore a real local git working tree.

Git is the resource a coding agent touches most, and *locally* it is natively
reversible — which makes it a clean fit for the snapshot -> apply -> restore
protocol against machinery unlike SQL savepoints or filesystem copies:

  * ``snapshot`` records the current ``HEAD`` commit, a stash object capturing
    the dirty *tracked* state (``git stash create``), and a copy of the
    *untracked* files (git's stash does not include them);
  * ``apply`` runs the agent's git operation (commit, branch, merge, rebase, or
    a destructive ``reset --hard`` / ``checkout``);
  * ``restore`` reverts with ``git reset --hard <head>`` + ``git clean`` + a
    re-apply of the dirty stash + restoration of the backed-up untracked files.

Because git keeps unreachable commits in the reflog, even a history the agent
*destroyed* (``reset --hard HEAD~5``) is recoverable — ``reset --hard`` back to
the recorded SHA brings every commit back.

**Honest boundary.** Pushing to a remote leaves the machine and cannot be
cleanly un-pushed, so it is NOT reversible. This adapter governs the LOCAL
repository only; a ``git push`` belongs on the irreversible / commit-gate lane
(a separate resource), exactly as the architecture's two-lane model prescribes.
``supports_rollback()`` is therefore ``True`` for everything this adapter
handles — and the push boundary is enforced elsewhere, not pretended here.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# Sentinel for read_version on a repo with no commits yet (mirrors the
# filesystem adapter's _FS_MISSING — a non-None marker so isolation can tell
# "read as empty" apart from a real SHA via a plain != comparison).
_GIT_EMPTY = "__no_head__"


class GitError(RuntimeError):
    """A git subprocess exited non-zero. Carries the stderr for the journal."""


class GitHandle:
    """The per-effect git handle injected as the first arg of git tools.

    Exposes ``run`` — execute one git command, rooted at the repo — which is the
    surface an agent's ``run_git`` tool calls. The command is the string a model
    naturally writes (e.g. ``"reset --hard HEAD~2"``, ``"commit -m 'wip'"``); it
    is split with :func:`shlex.split` (shell-safe — no shell is invoked) and
    passed to ``git`` as an argv list, so there is no shell-injection surface.
    """

    def __init__(self, repo_root: Path):
        self._root = repo_root

    def run(self, command: str) -> str:
        """Run ``git <command>`` in the repo; return stdout (raise on failure)."""
        argv = shlex.split(command)
        return _git(self._root, *argv)


def _git(repo_root: Path, *args: str, check: bool = True) -> str:
    """Run a git command rooted at ``repo_root``; return stripped stdout.

    No shell is spawned (argv form), so the agent-supplied command cannot inject
    shell syntax. On non-zero exit with ``check`` set, raises :class:`GitError`
    carrying stderr so the failure is legible in the journal / to the model.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


class GitAdapter:
    """``ResourceAdapter`` over a local git working tree."""

    name = "git"

    def __init__(self, repo_root: Path | str):
        self._root = Path(repo_root).resolve()
        self._backup_root: Path | None = None

    @property
    def root(self) -> Path:
        return self._root

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (TransactionalResourceAdapter) ---------

    def begin(self) -> None:
        # A per-txn scratch dir for the untracked-file backups (git's stash
        # captures tracked changes only, so untracked files are copied aside).
        self._backup_root = Path(tempfile.mkdtemp(prefix="pherix_git_"))

    def commit(self) -> None:
        self._cleanup_backup_root()

    def rollback(self) -> None:
        self._cleanup_backup_root()

    def _cleanup_backup_root(self) -> None:
        if self._backup_root is not None:
            shutil.rmtree(self._backup_root, ignore_errors=True)
            self._backup_root = None

    # --- per-effect snapshot / apply / restore ------------------------------

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        if self._backup_root is None:
            raise RuntimeError(
                "GitAdapter.snapshot() called outside a transaction; begin() "
                "must be called first."
            )
        head = _git(self._root, "rev-parse", "HEAD", check=False) or None
        # `git stash create` records the dirty *tracked* state as a commit
        # object without touching the worktree; empty output means clean.
        stash = _git(self._root, "stash", "create", check=False) or ""
        # Untracked, non-ignored files are not in the stash — back them up.
        untracked = [
            p
            for p in _git(
                self._root, "ls-files", "--others", "--exclude-standard"
            ).splitlines()
            if p
        ]
        backup_dir = self._backup_root / f"e_{effect.index}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for rel in untracked:
            src = self._root / rel
            dst = backup_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.copy2(src, dst)
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={
                "head": head,
                "stash": stash,
                "backup_dir": str(backup_dir),
                "untracked": untracked,
            },
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The handle is injected as the tool's first positional arg; the @tool
        # wrapper hides it from the agent's call-site (same shape as the FS adapter).
        return tool_fn(GitHandle(self._root), **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        payload = handle.payload
        head = payload.get("head")
        # 1. Tracked state + HEAD back to the snapshot commit (reflog-recoverable,
        #    so even commits the agent "destroyed" via reset --hard return).
        if head:
            _git(self._root, "reset", "--hard", head)
        # 2. Remove every untracked file/dir (those the agent created after the
        #    snapshot, plus the snapshot-time ones — restored from backup next).
        _git(self._root, "clean", "-fd", check=False)
        # 3. Restore the untracked files that existed at snapshot time.
        backup_dir = Path(payload["backup_dir"])
        for rel in payload.get("untracked", []):
            src = backup_dir / rel
            dst = self._root / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        # 4. Re-apply the dirty tracked changes captured at snapshot time.
        stash = payload.get("stash")
        if stash:
            _git(self._root, "stash", "apply", stash, check=False)

    # --- isolation (VersionedResourceAdapter) -------------------------------
    #
    # Git tools record no per-key read/write sets, so the commit-time isolation
    # diff never queries these for a git effect. They are provided (returning the
    # current HEAD as the repo's version tag) so the adapter conforms honestly
    # rather than being a partial implementation.

    def read_version(self, key: tuple) -> object:
        return _git(self._root, "rev-parse", "HEAD", check=False) or _GIT_EMPTY

    def write_version(self, key: tuple) -> object:
        return _git(self._root, "rev-parse", "HEAD", check=False) or _GIT_EMPTY
