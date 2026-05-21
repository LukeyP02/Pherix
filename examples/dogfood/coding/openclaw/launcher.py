"""Run OpenClaw's *built-in* file/bash actions through the Pherix sandbox — B2.

OpenClaw's ``read``/``write``/``edit`` and ``bash``/``process`` tools are
built-ins: they never travel over MCP, so the gateway (B1) cannot see them. They
are governed instead at the **environment level**, exactly like any other
out-of-box coding CLI, by the agent-agnostic sandbox in
``examples/dogfood/coding/sandbox.py``:

  * **Filesystem** — OpenClaw's workspace is rooted on the Pherix copy-on-write
    overlay (:class:`FilesystemAdapter`), so every file write/edit is a
    journalled, snapshotted, policy-gated, audited effect that rolls back if the
    session unwinds.
  * **PATH** — the ``git`` and ``sh`` shims are planted *first* on the ``PATH``
    OpenClaw's bash tool inherits, so when OpenClaw shells out to ``git push`` or
    ``rm -rf`` the OS resolves *our* shim, which forwards the argv into the same
    Pherix transaction instead of the real binary.

Reconciling with OpenClaw's own sandbox backend
-----------------------------------------------
OpenClaw ships its own sandbox framework (Docker default / SSH / OpenShell), so
Pherix must run *with* it, not fight it. Two workable arrangements; this launcher
implements the first and documents the second:

  1. **OpenShell / local backend (what this launcher sets up).** Configure
     OpenClaw to use its OpenShell (host-local) backend and launch it from this
     prepared environment: cwd = the Pherix CoW root, ``PATH`` = shims-first.
     OpenClaw's built-ins then operate on the governed root and its shell calls
     hit our shims. Lowest friction, no image building, and it reuses
     :func:`sandbox_env` verbatim.
  2. **Docker backend.** Bake the two shims into the OpenClaw sandbox image
     (copy them to ``/usr/local/bin`` ahead of the real ``git``/``sh`` on the
     image ``PATH``) and bind-mount the CoW root as the workspace. Heavier, but
     it keeps OpenClaw's container isolation. Out of scope to build here; the
     mechanism is identical once the shims are first on ``PATH`` inside the
     container.

Honest limit (shared with the sandbox README)
----------------------------------------------
The full **cross-process re-attach** — a shim invoked by OpenClaw in a *separate*
process routing back into this launcher's live in-memory transaction — is the
not-yet-built piece. ``route-cli`` currently records the intercepted argv rather
than mutating the running txn. So today this launcher proves the *wiring*
(governed root + shims-first PATH + session pointer), and the deterministic proof
of the routing *mechanism* is ``tests/test_dogfood_coding.py``. The capstone
(``docs/operator/airgapped-capstone.md``) is the manual end-to-end run.

Usage::

    # Prepare a disposable governed sandbox and print how to point OpenClaw at it:
    python -m examples.dogfood.coding.openclaw.launcher

    # Same, but actually exec OpenClaw inside the prepared environment
    # (operator's machine; OpenClaw must be installed):
    python -m examples.dogfood.coding.openclaw.launcher --run -- openclaw run "fix the bug"
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from examples.dogfood.coding.openclaw import OPENCLAW_IDENTITY
from examples.dogfood.coding.sandbox import Sandbox, sandbox_env
from examples.dogfood.infra import scratch_repo

# A seed workspace for the disposable run — a tiny repo OpenClaw can act on.
_SEED = {
    "src/app.py": "def main():\n    print('v1')\n",
    "README.md": "# disposable sandbox repo\n",
}


@contextmanager
def governed_sandbox(
    *,
    client_id: str = OPENCLAW_IDENTITY,
    seed: dict[str, str] | None = None,
) -> Iterator[tuple[Sandbox, dict[str, str], Path]]:
    """Stand up a governed sandbox + the environment OpenClaw should run under.

    Yields ``(sandbox, env, root)``: a live :class:`Sandbox` session, the
    process environment with the shims planted first on ``PATH`` and the session
    pointer set (``env``), and the CoW workspace root (``root``). OpenClaw is
    launched with ``cwd=root`` and ``env=env`` so its built-ins land on the
    governed tree and its shell calls hit our shims. On exit the session unwinds
    (reversible FS edits restored) and the scratch repo is removed.
    """
    seed = _SEED if seed is None else seed
    with scratch_repo(seed) as root:
        sandbox = Sandbox(root=root, client_id=client_id)
        bin_dir = root / ".pherix-bin"
        with sandbox.session():
            with sandbox_env(sandbox, bin_dir) as env:
                yield sandbox, env, root


def _print_instructions(env: dict[str, str], root: Path) -> None:
    """Print the operator-facing setup: the governed env + the OpenClaw command."""
    path_head = env["PATH"].split(os.pathsep)[0]
    print("=" * 72)
    print("Pherix governed sandbox is live — point OpenClaw at it")
    print("=" * 72)
    print(f"  workspace root (CoW overlay) : {root}")
    print(f"  shims first on PATH          : {path_head}")
    print(f"  session pointer              : {env.get('PHERIX_SANDBOX_SESSION')}")
    print()
    print("Run OpenClaw with its OpenShell (host-local) backend so its built-in")
    print("file/bash actions operate on this governed root and its shell calls")
    print("resolve the Pherix git/sh shims. Launch it with:")
    print()
    print(f"    cd {root}")
    print(f"    PATH={path_head}:$PATH \\")
    print(f"    PHERIX_SANDBOX_SESSION={env.get('PHERIX_SANDBOX_SESSION')} \\")
    print("    openclaw run \"<your task>\"   # OpenClaw on a LOCAL model")
    print()
    print("Point OpenClaw's `agent.model` at your local endpoint (e.g.")
    print("`openai/qwen2.5-coder:7b` against http://localhost:11434/v1) for the")
    print("air-gapped run — see docs/operator/airgapped-capstone.md.")
    print("=" * 72)


def _run_openclaw(argv: list[str], env: dict[str, str], root: Path) -> int:
    """Exec the operator's OpenClaw command inside the prepared environment."""
    import subprocess

    if not argv:
        print("launcher: --run needs a command after `--`", file=sys.stderr)
        return 2
    print(f"[launcher] exec in governed sandbox ({root}): {' '.join(argv)}")
    proc = subprocess.run(argv, env=env, cwd=str(root))
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    run_cmd: list[str] | None = None
    if args and args[0] == "--run":
        rest = args[1:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        run_cmd = rest
    elif args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    elif args:
        print(f"launcher: unknown arguments {args!r}", file=sys.stderr)
        return 2

    with governed_sandbox() as (_sandbox, env, root):
        if run_cmd is not None:
            return _run_openclaw(run_cmd, env, root)
        _print_instructions(env, root)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
