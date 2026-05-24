"""CLI for the enterprise-robustness sweep: ``python -m examples.dogfood.sims.enterprise``.

A thin wiring layer — all the classifier and rendering logic lives in the shared
:mod:`examples.dogfood.sims.robustness` (so the devops and local-airgap flagships
reuse it without importing this enterprise package). This module only builds the
enterprise scenarios from :mod:`fixtures` and hands them to the shared runner.

    python -m examples.dogfood.sims.enterprise --runs 50 -v --out reports/

needs a real API key (it runs a live model through the two-arm runner). The
offline test suite exercises the classifier directly and never reaches here.
"""

from __future__ import annotations

from examples.dogfood.sims import robustness
from examples.dogfood.sims.enterprise import fixtures


def main(argv: list[str] | None = None) -> None:
    parser = robustness.build_parser(
        description=(
            "Enterprise-robustness rollup: one fixed regulated-data-ops agent, "
            "swept across many situations governed-vs-ungoverned, folded into the "
            "2×2-plus-edge-cells classification an enterprise security team reads."
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
