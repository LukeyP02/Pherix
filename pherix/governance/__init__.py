"""Governance — the serialisable policy spec, the dry-run preview, the templates.

This package is the *load* side of the governance UI (``site/governance.html``).
The UI composes a :class:`PolicySpec` visually, serialises it to JSON, and the
buyer ships that JSON. :func:`from_spec` turns the same JSON back into a real
:class:`pherix.core.policy.Policy` the engine runs unchanged; :func:`preview`
folds a candidate spec over a sample journal via the *real* engine
(:meth:`Policy.collect_verdicts`) so the verdicts the UI shows are the verdicts
the runtime produces — that equivalence is load-bearing and pinned by tests.

The whole package is one idea: a policy is data, not code. The Slice-6 ``Policy``
is a bundle of live Python callables (caps with ``via`` lambdas, rule closures);
that cannot be saved, diffed, or loaded into a browser. The spec is the
serialisable shadow of that bundle — rich enough to round-trip the common cases
(allow/deny, count/sum caps, the human gate, a small catalog of world-state rule
templates), honest about the edge cases it defers to hand-written Python.
"""

from __future__ import annotations

from pherix.governance.preview import (
    EffectScenario,
    EffectVerdict,
    PreviewResult,
    preview,
)
from pherix.governance.spec import (
    CapSpec,
    PolicySpec,
    RuleSpec,
    from_spec,
    to_python,
    to_spec,
)
from pherix.governance.templates import STARTER_TEMPLATES, TEMPLATE_REGISTRY

__all__ = [
    "CapSpec",
    "EffectScenario",
    "EffectVerdict",
    "PolicySpec",
    "PreviewResult",
    "RuleSpec",
    "STARTER_TEMPLATES",
    "TEMPLATE_REGISTRY",
    "from_spec",
    "preview",
    "to_python",
    "to_spec",
]
