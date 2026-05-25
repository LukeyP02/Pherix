"""The devops-robustness sim: one governed SRE/release agent, many situations.

A single **release/SRE agent** — fixed system prompt, fixed toolset, fixed SRE
policy (:mod:`agent`) — dropped into several distinct situations
(:mod:`fixtures`) and run governed-vs-ungoverned at N. The shared robustness
runner folds the per-fixture :class:`~examples.dogfood.sims.scenario.ScenarioResult`s
into the 2×2-plus-edge-cells classification a security team reads: where Pherix
wasn't needed, where it caught the harm, where harm escaped, and where it
spuriously blocked clean work.

This is the **second domain flagship**. It deliberately mirrors the
enterprise-robustness sim's *structure* (one frozen agent, a region of fixtures,
the matched two-arm sweep) over a completely **different resource set** — git
history, the filesystem, a production database, and cloud infrastructure — to
prove the same engine governs whatever an agent can break, not just one domain.

Like the enterprise subpackage, this exposes no module-level ``SCENARIO``: the
fixtures are produced by :func:`fixtures.make_scenario` and driven by the
robustness runner, not by the generic ``all_scenarios()`` discovery, so the
devops fixtures never pollute the single-domain sim suite.
"""
