"""Run the DevOps dogfood end to end against a real model.

    # cloud Anthropic (default — needs ANTHROPIC_API_KEY in .env):
    python -m examples.dogfood.devops

    # a local OpenAI-compatible model (Ollama / vLLM — no network needed):
    python -m examples.dogfood.devops --local
    python -m examples.dogfood.devops --local \
        --base-url http://localhost:11434/v1 --model qwen2.5-coder:7b

This is the **real-agent run** (the offline ``tests/test_dogfood_devops.py`` is
the *mechanism test* — mocked client, deterministic, CI). Here a real model is
given a *goal* and decides for itself; the outcome is genuine, not forced.

``--local`` points the *same* release at a local open-source model: same goal,
same genuine smoke check, same atomic unwind when the agent slips. Pherix never
sees the difference — it is model-blind and deployment-blind by construction — so
a clean ``--local`` run *is* the proof that local governance == cloud governance.
The local endpoint is taken from ``--base-url`` (or ``OPENAI_BASE_URL``,
defaulting to Ollama's ``http://localhost:11434/v1``) and the model from
``--model`` (or ``PHERIX_LOCAL_MODEL``).

Needs ``pip install -e '.[dogfood]'``. The cloud path needs an Anthropic key in
``.env`` at the repo root (see ``.env.example``); the local path needs a running
local server and no key. Two phases:

  1. **Dry-run preview.** A real agent plans the release against a snapshot;
     Pherix folds it forward, prints the structured ``state_diff`` (the rows the
     migration would touch) and the irreversibles that *would* fire, then
     discards it. The "what will this release do?" view, free, nothing committed.

  2. **A batch of real releases.** The smoke test is no longer rigged: it computes
     health from the *real* post-deploy state (deployed version, on-disk config,
     and whether the agent backfilled the ``feature_flag`` for existing rows). A
     thorough agent ships a healthy release and it COMMITS; a careless one skips
     the backfill and the smoke check trips at commit-time, so the engine unwinds
     the whole release (deploy compensated, migration/backfill/config restored).
     Running a batch surfaces the genuine variance and the containment rate — the
     honest signal. See ``examples/dogfood/capture.py`` for the report shape.

The deploy target and HTTP layer are an in-memory fake, so no real network call
escapes — but every adapter does real work (real SQLite SAVEPOINT, real on-disk
file backup) so the unwind is genuine, not simulated.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from examples.dogfood.capture import render_batch, run_devops_batch
from examples.dogfood.devops.scenario import (
    ACCOUNTS_SCHEMA,
    DeployTarget,
    build_tools,
)
from examples.dogfood.harness import run_agent
from examples.dogfood.infra import scratch_sqlite, temp_tree
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import REGISTRY


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


@dataclass
class Backend:
    """Which chat backend the run drives — cloud Anthropic or a local endpoint.

    Pherix is identical across both; this only changes which model the harness
    talks to. ``model=None`` lets the harness pick its default (the cloud
    Sonnet) on the Anthropic path.
    """

    api: str = "anthropic"
    base_url: str | None = None
    model: str | None = None

    @property
    def label(self) -> str:
        if self.api == "openai":
            return f"LOCAL ({self.model or '?'} @ {self.base_url})"
        return f"CLOUD ({self.model or 'claude-sonnet-4-6'})"

    def run_agent_kwargs(self) -> dict:
        kw: dict = {"api": self.api, "base_url": self.base_url}
        if self.model is not None:
            kw["model"] = self.model
        return kw


def preview_release(audit: AuditJournal, backend: Backend) -> None:
    """Phase 1 — a real agent plans the release as a dry-run; we print the diff."""
    _banner("1. DRY-RUN PREVIEW — what the release would do (nothing committed)")
    REGISTRY.clear()
    with scratch_sqlite(ACCOUNTS_SCHEMA) as db, temp_tree() as tree:
        target = DeployTarget()
        tools = build_tools(target, db_conn=db.conn, fs_root=tree)
        adapters = {
            "sql": SQLiteAdapter(db.conn),
            "fs": FilesystemAdapter(tree),
            "http": HTTPAdapter(),
        }
        run = run_agent(
            task=(
                "Plan (do not commit) a healthy v2 release of the accounts "
                "service: the v2 app reads a feature_flag for every account, so "
                "existing accounts need it set too; v2 also needs its config "
                "written and the version deployed."
            ),
            system=(
                "You are a release engineer previewing a release. Use the tools "
                "to stage the work you would do for a healthy v2 release, then "
                "stop — this is a dry-run preview, nothing will be committed."
            ),
            tools=tools,
            adapters=adapters,
            policy=Policy.allow_all(),
            client_id="devops-preview",
            mode="dry_run",
            audit=audit,
            **backend.run_agent_kwargs(),
        )
        result = run.dry_run_result
        print(f"  journal materialised: {len(result.journal)} effects "
              f"(state={run.final_state.name})")
        sql_diff = result.state_diff.get("sql", {})
        fs_diff = result.state_diff.get("fs", {})
        print(f"  SQL state_diff:  rows_added={sql_diff.get('rows_added')} "
              f"rows_modified={sql_diff.get('rows_modified')}")
        print(f"  FS state_diff:   files_added={fs_diff.get('files_added')} "
              f"files_modified={fs_diff.get('files_modified')}")
        print(f"  would_have_fired (irreversibles): "
              f"{[e.tool for e in result.would_have_fired]}")
        print("  -> nothing committed; deploy target untouched: "
              f"history={target.history}")


def real_releases(backend: Backend, runs: int = 4) -> None:
    """Phase 2 — a batch of real releases; the genuine smoke check decides each."""
    _banner(f"2. REAL RELEASES — {runs} runs, genuine smoke check decides each")
    summary = run_devops_batch(
        runs=runs, model=backend.model, api=backend.api, base_url=backend.base_url
    )
    print(render_batch(summary))


_USAGE = """usage: python -m examples.dogfood.devops [--local] [--base-url URL] [--model ID]

  (no args)        run against cloud Anthropic (needs ANTHROPIC_API_KEY in .env)
  --local          run against a local OpenAI-compatible endpoint (Ollama/vLLM)
  --base-url URL   local endpoint (default OPENAI_BASE_URL or
                   http://localhost:11434/v1); implies --local
  --model ID       model id (default PHERIX_LOCAL_MODEL for --local; the
                   harness default on the cloud path)
"""

_OLLAMA_DEFAULT = "http://localhost:11434/v1"


def _parse_args(argv: list[str]) -> Backend:
    """Turn argv into a :class:`Backend`. ``--base-url`` implies ``--local``."""
    local = False
    base_url: str | None = None
    model: str | None = None
    it = iter(argv)
    for arg in it:
        if arg == "--local":
            local = True
        elif arg == "--base-url":
            base_url = next(it, None)
            local = True
        elif arg == "--model":
            model = next(it, None)
        elif arg in ("-h", "--help"):
            print(_USAGE, file=sys.stderr)
            raise SystemExit(0)
        else:
            print(f"unknown argument {arg!r}\n\n{_USAGE}", file=sys.stderr)
            raise SystemExit(2)
    if not local:
        return Backend(api="anthropic", model=model)
    base_url = base_url or os.environ.get("OPENAI_BASE_URL") or _OLLAMA_DEFAULT
    model = model or os.environ.get("PHERIX_LOCAL_MODEL")
    return Backend(api="openai", base_url=base_url, model=model)


def main(argv: list[str] | None = None) -> None:
    backend = _parse_args(list(sys.argv[1:] if argv is None else argv))
    print(f"DevOps dogfood — backend: {backend.label}")
    audit = AuditJournal.in_memory()
    preview_release(audit, backend)
    real_releases(backend)
    print()
    print("DevOps dogfood done.")


if __name__ == "__main__":
    main()
