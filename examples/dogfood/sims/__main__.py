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
    for name, scn in chosen.items():
        res = run_scenario(scn, runs=args.runs, model=model, api=api, base_url=base_url)
        print(render_scenario(res))
        if args.out:
            write_scenario(res, args.out)
        results.append(res)

    if len(results) > 1:
        print(render_grand_total(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
