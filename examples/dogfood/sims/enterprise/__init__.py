"""The enterprise-robustness sim: one governed agent, many situations.

A single **regulated-data-operations agent** — fixed system prompt, fixed
toolset, fixed enterprise policy (:mod:`agent`) — dropped into several distinct
situations (:mod:`fixtures`) and run governed-vs-ungoverned at N. The
:mod:`robustness` runner folds the per-fixture :class:`ScenarioResult`s into the
2×2-plus-edge-cells classification an enterprise security team actually reads:
where Pherix wasn't needed, where it caught the harm, where harm escaped, and
where it spuriously blocked clean work.

This subpackage deliberately exposes no module-level ``SCENARIO``: the fixtures
are produced by the :func:`fixtures.make_scenario` factory and driven by the
robustness runner, not by the generic ``all_scenarios()`` discovery. The
runtime's discovery walk imports this package's ``__init__`` and finds no
``SCENARIO`` here, so the enterprise fixtures never pollute the single-domain
sim suite.
"""
