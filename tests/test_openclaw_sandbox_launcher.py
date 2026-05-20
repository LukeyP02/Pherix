"""Offline proof that the OpenClaw B2 launcher wires the governed environment.

We don't launch a real OpenClaw daemon (it isn't on CI, and the cross-process
re-attach is the documented not-yet-built piece). What B2 must guarantee is the
*wiring*: OpenClaw's workspace is rooted on the Pherix CoW overlay, the git/sh
shims are first on the PATH OpenClaw inherits, and a live session pointer is
exported. We assert exactly that, and that the sandbox session the launcher
opens governs a routed built-in action — reusing the same Sandbox.route the
deterministic mechanism test (tests/test_dogfood_coding.py) covers.
"""

from __future__ import annotations

import os
from pathlib import Path

from examples.dogfood.coding.openclaw import OPENCLAW_IDENTITY
from examples.dogfood.coding.openclaw.launcher import governed_sandbox
from examples.dogfood.coding.sandbox import VERB_GIT, VERB_WRITE


def test_launcher_roots_workspace_on_cow_and_shims_first_on_path():
    with governed_sandbox() as (sandbox, env, root):
        # Workspace is the governed CoW root, attributed to OpenClaw.
        assert sandbox.root == Path(root).resolve()
        assert sandbox.client_id == OPENCLAW_IDENTITY

        # The shims dir is FIRST on the PATH OpenClaw's bash tool inherits, and
        # it actually contains executable git/sh shims.
        path_head = env["PATH"].split(os.pathsep)[0]
        assert path_head.endswith(".pherix-bin")
        for binary in ("git", "sh"):
            shim = Path(path_head) / binary
            assert shim.exists() and os.access(shim, os.X_OK)

        # A live session pointer is exported so a shim invocation can find it.
        assert env.get("PHERIX_SANDBOX_SESSION")
        assert Path(env["PHERIX_SANDBOX_SESSION"]).exists()


def test_launcher_session_governs_a_routed_builtin():
    with governed_sandbox() as (sandbox, _env, root):
        # An allowed built-in write under src/ is journalled + applied.
        out = sandbox.route(VERB_WRITE, path="src/app.py", content="def main():\n    print('v2')\n")
        assert out.allowed and out.journalled
        assert (Path(root) / "src" / "app.py").read_text().endswith("print('v2')\n")

        # A push to main is GATED by the same coding policy — OpenClaw's shelled
        # `git push origin main` would be refused exactly here.
        gated = sandbox.route(VERB_GIT, argv=["push", "origin", "main"])
        assert gated.allowed is False
        assert "push" in gated.detail.lower()

        sandbox._ctx.rollback()  # keep the scratch tree pristine
