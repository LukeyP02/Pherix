"""DevOps dogfood — a real agent performs a release; a failing smoke test
unwinds the whole thing atomically.

A real LLM is handed four Pherix-wrapped domain tools and asked to ship a
release: migrate the schema, write the config, deploy, then smoke-test the
deployment. The release is *engineered to fail its smoke test* — and that
single failure pulls the whole release back to its pre-release state:

  - the schema migration (reversible SQL) rolls back via its SAVEPOINT,
  - the config write (reversible filesystem) restores from its backup,
  - the deploy (irreversible HTTP, already fired) is compensated by its
    registered ``rollback_deploy`` tool.

The mechanism is the engine's commit-time mixed-fold unwind
(``TxnContext._partial_unwind`` in ``pherix/core/runtime.py``). See
``run_release`` for exactly how the smoke failure triggers it.
"""

from examples.dogfood.devops.scenario import (
    DeployTarget,
    build_tools,
    run_release,
)

__all__ = ["DeployTarget", "build_tools", "run_release"]
