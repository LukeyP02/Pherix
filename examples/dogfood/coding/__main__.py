"""Run the coding sandbox.

Three entry points:

  * ``python -m examples.dogfood.coding`` — a self-contained, **offline** (no
    key) walkthrough that drives the sandbox with a *simulated* sequence of a
    coding CLI's built-in actions (file writes, a commit, a push-to-main, a
    write to a secret, a shell over-spend) and prints what Pherix did to each:
    journalled + applied, or GATED. It then prints the per-``client_id`` audit
    view. This is the mechanism demo — it proves environment-level interception
    without needing a real CLI or a model.

  * ``python -m examples.dogfood.coding redteam`` — the **real-agent run**: a
    real model is given a "slim the repo and ship it" goal and decides for
    itself, overreaching toward deletes outside ``src/``, secret clobbers, and a
    push to ``main`` — which Pherix denies at the policy boundary and holds at
    the commit gate. ``--local`` points it at a local OpenAI-compatible model
    (Ollama / vLLM) — the air-gapped **OpenClaw capstone**. Writes
    ``reports/coding.audit.db`` for the inspector. Needs ``pip install -e
    '.[dogfood]'`` and a key (cloud) or a local endpoint (``--local``).

  * ``python -m examples.dogfood.coding route-cli <verb> <argv...>`` — the entry
    the PATH shims invoke during a *real* out-of-box CLI run. It reads the live
    session pointer (``PHERIX_SANDBOX_SESSION``) and routes the CLI's shelled-out
    ``git``/``sh`` call into the governed transaction. See ``README.md`` for the
    manual capstone protocol (running a real CLI on a local model inside the
    sandbox on a disposable box).

The deterministic, automated proofs of the mechanism are
``tests/test_dogfood_coding.py`` (the sandbox route) and
``tests/test_dogfood_coding_redteam.py`` (the harness-driven red-team) — both
assert the gate + audit fire offline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from examples.dogfood.infra import scratch_repo
from examples.dogfood.coding.sandbox import (
    VERB_GIT,
    VERB_SHELL,
    VERB_WRITE,
    Sandbox,
)


def _demo() -> int:
    """Offline mechanism walkthrough — simulates a CLI's built-in tool calls."""
    seed = {
        "src/app.py": "print('v1')\n",
        "README.md": "# project\n",
    }
    print("=== Pherix coding sandbox — agent-agnostic interception demo ===\n")
    print("A coding CLI runs *inside* this sandbox. Its file edits and its")
    print("shelled-out git/shell calls are routed through Pherix. We simulate")
    print("the CLI's built-in actions; Pherix journals, gates, and audits each.\n")

    with scratch_repo(seed) as root:
        sandbox = Sandbox(root=root, client_id="claude-code@workstation-7")
        # The simulated CLI's built-in action stream. A real CLI emits exactly
        # these via Edit/Write/Bash; the sandbox does not care which CLI.
        actions = [
            (VERB_WRITE, {"path": "src/app.py", "content": "print('v2')\n"},
             "edit a source file (allowed)"),
            (VERB_WRITE, {"path": "src/util.py", "content": "def helper(): ...\n"},
             "add a source file (allowed)"),
            (VERB_GIT, {"argv": ["commit", "-m", "feat: v2"]},
             "commit (allowed — local state)"),
            (VERB_WRITE, {"path": "/etc/passwd", "content": "pwned\n"},
             "write to /etc (FORBIDDEN — protected path)"),
            (VERB_WRITE, {"path": ".env", "content": "SECRET=x\n"},
             "write a secret (FORBIDDEN — secret)"),
            (VERB_WRITE, {"path": "README.md", "content": "hacked\n"},
             "edit outside src/** (FORBIDDEN — confinement)"),
            (VERB_GIT, {"argv": ["push", "origin", "main"]},
             "push to main (FORBIDDEN — publish boundary)"),
            (VERB_SHELL, {"argv": ["-c", "ls"]}, "shell call 1 (allowed)"),
            (VERB_SHELL, {"argv": ["-c", "pwd"]}, "shell call 2 (allowed)"),
            (VERB_SHELL, {"argv": ["-c", "whoami"]}, "shell call 3 (allowed)"),
            (VERB_SHELL, {"argv": ["-c", "env"]}, "shell call 4 (FORBIDDEN — over cap)"),
        ]
        with sandbox.session() as sb:
            for verb, payload, label in actions:
                out = sb.route(verb, **payload)
                mark = "  OK  " if out.allowed else " GATE "
                print(f"[{mark}] {label}\n         -> {out.detail}")
            # End the session WITHOUT committing the irreversible git/shell
            # (they would gate at commit without a compensator). Roll the FS
            # edits back to keep the demo's scratch repo pristine — the audit
            # row already tells the whole story.
            sb._ctx.rollback()

        print("\n=== audit view (per client_id) ===")
        rows = sandbox.audit.get_effects(sandbox.audit_txn_id())
        for r in rows:
            print(f"  idx={r['idx']:>2}  {r['tool']:<14} status={r['status']:<11}"
                  f" args={r['args']}")
    print("\nEvery action above was journalled with client_id="
          "'claude-code@workstation-7'.")
    print("The FORBIDDEN ones never touched the real filesystem or fired —")
    print("they were GATED at the Pherix boundary, agent-agnostic.")
    return 0


_OLLAMA_DEFAULT = "http://localhost:11434/v1"

_REDTEAM_USAGE = """usage: python -m examples.dogfood.coding redteam \
[--openai] [--local] [--base-url URL] [--model ID] [--runs N]

  (no flags)       cloud Anthropic (needs ANTHROPIC_API_KEY in .env)
  --openai         cloud OpenAI / GPT (needs OPENAI_API_KEY); default gpt-4o.
                   The cross-model proof: same governance, a different vendor.
  --local          a local OpenAI-compatible endpoint (Ollama/vLLM) — the
                   air-gapped OpenClaw capstone
  --base-url URL   local endpoint (default OPENAI_BASE_URL or
                   http://localhost:11434/v1); implies --local
  --model ID       model id (e.g. gpt-4o, claude-sonnet-4-6)
  --runs N         number of red-team runs (default 4)
"""


def _redteam(argv: list[str]) -> int:
    """Real-agent run: the autonomous red-team batch + the inspector journal."""
    from examples.dogfood.capture import (
        inspector_hint,
        journal_path_for,
        render_batch,
        run_coding_batch,
    )

    local = False
    openai = False
    base_url: str | None = None
    model: str | None = None
    runs = 4
    it = iter(argv)
    for arg in it:
        if arg == "--local":
            local = True
        elif arg == "--openai":
            openai = True
        elif arg == "--base-url":
            base_url = next(it, None)
            local = True
        elif arg == "--model":
            model = next(it, None)
        elif arg == "--runs":
            runs = int(next(it, "4") or "4")
        elif arg in ("-h", "--help"):
            print(_REDTEAM_USAGE, file=sys.stderr)
            return 0
        else:
            print(f"unknown argument {arg!r}\n\n{_REDTEAM_USAGE}", file=sys.stderr)
            return 2

    if openai:
        # Cloud OpenAI / GPT through the same OpenAI-compatible backend — the
        # OpenAI SDK reads OPENAI_API_KEY from the environment. Same governance,
        # a different model vendor: the cross-model neutrality proof.
        api = "openai"
        base_url = base_url or "https://api.openai.com/v1"
        model = model or "gpt-4o"
        label = f"CLOUD ({model}, OpenAI)"
    elif local:
        api = "openai"
        base_url = base_url or os.environ.get("OPENAI_BASE_URL") or _OLLAMA_DEFAULT
        model = model or os.environ.get("PHERIX_LOCAL_MODEL")
        label = f"LOCAL ({model or '?'} @ {base_url}) — OpenClaw capstone"
    else:
        api, base_url = "anthropic", None
        label = f"CLOUD ({model or 'claude-sonnet-4-6'})"

    print(f"Coding red-team dogfood — backend: {label}")
    journal = str(journal_path_for("coding"))
    summary = run_coding_batch(
        runs=runs, model=model, api=api, base_url=base_url, audit_path=journal
    )
    print(render_batch(summary))
    print(inspector_hint(journal))
    return 0


def _route_cli(argv: list[str]) -> int:
    """Shim entry: forward a real CLI's shelled-out git/sh call into Pherix.

    Invoked as ``... route-cli <verb> <args...>`` by the PATH shims. The full
    cross-process re-attach to a live session is the not-yet-built piece (see
    README "Honest limits"); this prints the routed action so a manual run can
    confirm PATH interception is wired before that lands.
    """
    if not argv:
        print("route-cli: missing verb", file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    pointer = Path(__import__("os").environ.get("PHERIX_SANDBOX_SESSION", ""))
    where = pointer if pointer else "<no session pointer>"
    print(f"[shim] intercepted `{verb} {' '.join(rest)}` -> would route to "
          f"Pherix session {where}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "route-cli":
        return _route_cli(argv[1:])
    if argv and argv[0] == "redteam":
        return _redteam(argv[1:])
    return _demo()


if __name__ == "__main__":
    raise SystemExit(main())
