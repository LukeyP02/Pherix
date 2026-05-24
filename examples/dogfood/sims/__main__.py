"""Run the tail-risk sim suite.

    python -m examples.dogfood.sims                 # every scenario, 20 runs/arm
    python -m examples.dogfood.sims coding          # one scenario
    python -m examples.dogfood.sims --runs 100      # the headline N
    python -m examples.dogfood.sims --openai --model gpt-4o-mini   # force one backend

By **default each scenario runs on its own backend** (``Scenario.provider`` /
``Scenario.model``), so the mixed Claude+GPT fleet runs in one pass. ``--openai``
forces every scenario onto cloud GPT (a cross-model sweep); ``--model`` overrides
the model id for whichever backend is active.

Each scenario runs ``--runs`` times ungoverned then ``--runs`` times governed,
judged by the same independent harm oracle, and prints the natural disaster rate
vs the residual rate with Pherix. ``--out DIR`` writes full per-run JSON for
traceability. Needs the relevant key(s) in ``.env`` (``ANTHROPIC_API_KEY`` and/or
``OPENAI_API_KEY``) — the suite calls real models.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from examples.dogfood.sims.scenario import (
    all_scenarios,
    render_grand_total,
    render_scenario,
    run_scenario,
    write_scenario,
)


def main(argv: list[str] | None = None) -> int:
    scenarios = all_scenarios()
    parser = argparse.ArgumentParser(description="Pherix tail-risk simulation suite.")
    parser.add_argument(
        "scenario",
        nargs="?",
        default="all",
        help=f"scenario to run, or 'all' (available: {', '.join(scenarios)})",
    )
    parser.add_argument("--runs", type=int, default=20, help="runs per arm (default 20)")
    parser.add_argument("--model", default=None, help="model id override")
    parser.add_argument(
        "--openai",
        action="store_true",
        help="force every scenario onto cloud OpenAI/GPT (needs OPENAI_API_KEY; "
        "default gpt-4o-mini) — a cross-model sweep, overriding per-scenario providers",
    )
    parser.add_argument("--out", default=None, help="directory for per-run JSON")
    args = parser.parse_args(argv)

    if args.scenario != "all" and args.scenario not in scenarios:
        print(
            f"unknown scenario {args.scenario!r}; available: {', '.join(scenarios)} (or 'all')",
            file=sys.stderr,
        )
        return 2

    # Default: api/model = None -> each scenario runs on its own backend (the
    # mixed fleet). --openai forces cloud GPT across the board (a sweep).
    if args.openai:
        api: str | None = "openai"
        base_url: str | None = "https://api.openai.com/v1"
        model = args.model or "gpt-4o-mini"
    else:
        api = None
        base_url = None
        model = args.model

    chosen = scenarios if args.scenario == "all" else {args.scenario: scenarios[args.scenario]}
    results = []
    failed: list[tuple[str, str]] = []
    for name, scn in chosen.items():
        # Per-scenario fault isolation: run_arm already isolates each *run*, but a
        # failure in setup() or in a scenario's own run_arm_override (the
        # concurrent scenario) could still escape. Catch it here so one bad
        # scenario neither aborts the suite nor discards the scenarios already
        # done — their per-run JSON is written below, before the next scenario.
        try:
            res = run_scenario(scn, runs=args.runs, model=model, api=api, base_url=base_url)
        except Exception as exc:  # noqa: BLE001 — isolate, log, continue
            msg = f"{type(exc).__name__}: {exc}"
            print(f"  ✗ scenario {name!r} failed entirely — skipped: {msg}", file=sys.stderr)
            failed.append((name, msg))
            continue
        print(render_scenario(res))
        if args.out:
            write_scenario(res, args.out)
        results.append(res)

    if len(results) > 1:
        print(render_grand_total(results))
    if failed:
        print("\nSCENARIOS THAT FAILED ENTIRELY (re-run just these):", file=sys.stderr)
        for name, msg in failed:
            print(f"  ✗ {name}: {msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
