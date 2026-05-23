"""Run the tail-risk sim suite.

    python -m examples.dogfood.sims                 # every scenario, 20 runs/arm
    python -m examples.dogfood.sims insurance       # one scenario
    python -m examples.dogfood.sims --runs 100      # the headline N
    python -m examples.dogfood.sims --openai --model gpt-4o-mini   # cross-model

Each scenario runs ``--runs`` times ungoverned then ``--runs`` times governed,
judged by the same independent harm oracle, and prints the natural disaster rate
vs the residual rate with Pherix. ``--out DIR`` writes full per-run JSON for
traceability. Needs a key (``ANTHROPIC_API_KEY``, or ``OPENAI_API_KEY`` with
``--openai``) in ``.env`` — the suite calls a real model.
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
        help="drive via cloud OpenAI/GPT (needs OPENAI_API_KEY; default gpt-4o-mini)",
    )
    parser.add_argument("--out", default=None, help="directory for per-run JSON")
    args = parser.parse_args(argv)

    if args.scenario != "all" and args.scenario not in scenarios:
        print(
            f"unknown scenario {args.scenario!r}; available: {', '.join(scenarios)} (or 'all')",
            file=sys.stderr,
        )
        return 2

    api = "openai" if args.openai else "anthropic"
    base_url = "https://api.openai.com/v1" if args.openai else None
    model = args.model or ("gpt-4o-mini" if args.openai else None)

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
