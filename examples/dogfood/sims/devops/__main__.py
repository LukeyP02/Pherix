"""CLI for the devops-robustness sweep: ``python -m examples.dogfood.sims.devops``.

A thin wiring layer — all the classifier and rendering logic lives in the shared
:mod:`examples.dogfood.sims.robustness` (the same runner the enterprise flagship
uses), so this module only builds the devops scenarios from :mod:`fixtures` and
hands them to the shared runner. The rollup JSON is named from the fixtures'
``devops:`` prefix, so it lands as ``devops_rollup.json``.

    python -m examples.dogfood.sims.devops --runs 50 -v --out reports/

needs a real API key (it runs a live model through the two-arm runner). The
offline test suite exercises the classifier directly and never reaches here.
"""

from __future__ import annotations

from examples.dogfood.sims import robustness
from examples.dogfood.sims.devops import fixtures


def main(argv: list[str] | None = None) -> None:
    parser = robustness.build_parser(
        description=(
            "Devops-robustness rollup: one fixed release/SRE agent — with git, "
            "the filesystem, a production DB, and cloud infra — swept across many "
            "situations governed-vs-ungoverned, folded into the 2×2-plus-edge-"
            "cells classification a security team reads. The second domain "
            "flagship: the same engine governing a different resource set."
        )
    )
    args = parser.parse_args(argv)
    robustness.run_suite(
        fixtures.make_all(),
        benign_names=set(fixtures.BENIGN_FIXTURES),
        runs=args.runs,
        api=args.api,
        model=args.model,
        verbose=args.verbose,
        out=args.out,
    )


if __name__ == "__main__":
    main()
