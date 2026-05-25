"""Run the tail-risk sim suite.

    python -m examples.dogfood.sims                          # every scenario, 20 runs/arm
    python -m examples.dogfood.sims coding                   # one scenario
    python -m examples.dogfood.sims openclaw coding sre insurance   # the diametric four
    python -m examples.dogfood.sims --runs 100               # the headline N
    python -m examples.dogfood.sims --openai --model gpt-4o-mini    # force one backend

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
        nargs="*",
        default=[],
        help="one or more scenarios to run (space-separated), or omit for all "
        f"(available: {', '.join(scenarios)})",
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
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print each run's effect-journal trace (what the agent did + how the "
        "engine ended each effect) above the progress bar",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=0.0,
        help="seconds to sleep between runs (precautionary rate-limit spacing; "
        "0 = off — tier-2 limits don't need it)",
    )
    parser.add_argument(
        "--pressure",
        action="store_true",
        help="run the high-pressure (non-leading) prompt variant where the "
        "ungoverned harm rate is actually > 0 — the value headline, vs the benign "
        "default which is the 0-false-positives control",
    )
    args = parser.parse_args(argv)

    unknown = [s for s in args.scenario if s not in scenarios]
    if unknown:
        print(
            f"unknown scenario(s) {', '.join(unknown)!r}; available: "
            f"{', '.join(scenarios)} (or omit for all)",
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

    chosen = scenarios if not args.scenario else {s: scenarios[s] for s in args.scenario}
    results = []
    failed: list[tuple[str, str]] = []
    for name, scn in chosen.items():
        # A scenario with an external dependency (the air-gapped scenario needs a
        # local model server) may declare a preflight check. If it returns a
        # reason, skip this scenario with one honest line and carry on — far
        # cleaner than running N crashed runs against a dead endpoint.
        if scn.preflight is not None:
            reason = scn.preflight()
            if reason:
                print(f"ⓘ  skipped {name!r}: {reason}")
                continue
        # --pressure: swap in the high-pressure (non-leading) prompt variant for
        # BOTH arms (matched). This is where the ungoverned harm rate is actually
        # > 0; the benign default is the 0-false-positives control.
        if args.pressure:
            import dataclasses

            if scn.pressure is None:
                print(f"ⓘ  {name!r} has no pressure variant — running benign prompt")
            else:
                scn = dataclasses.replace(
                    scn, system=scn.pressure[0], task=scn.pressure[1]
                )
                print(f"⚡ {name!r}: high-pressure prompt (the value headline)")
        # Per-scenario fault isolation: run_arm already isolates each *run*, but a
        # failure in setup() or in a scenario's own run_arm_override (the
        # concurrent scenario) could still escape. Catch it here so one bad
        # scenario neither aborts the suite nor discards the scenarios already
        # done — their per-run JSON is written below, before the next scenario.
        try:
            res = run_scenario(
                scn,
                runs=args.runs,
                model=model,
                api=api,
                base_url=base_url,
                verbose=args.verbose,
                pace=args.pace,
            )
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
