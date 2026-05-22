"""Named rule templates + a starter library of ready-made policies.

Two registries:

- :data:`TEMPLATE_REGISTRY` — name → factory ``(**params) -> rule``. A spec's
  :class:`~pherix.governance.spec.RuleSpec` keys into this. Templates are *named*
  (not inline closures) for one reason: a name + params round-trips through JSON
  and can be reproduced by the browser's JS verdict mirror. Each template here
  therefore has a JS twin in ``site/policy-eval.js`` and the two are pinned
  identical by ``tests/test_governance_js_conformance.py``.
- :data:`STARTER_TEMPLATES` — the four ready-made :class:`PolicySpec`s a buyer
  copies so they start from a running example, not a blank page.

Adding a template is deliberately a two-language change: register the factory
here *and* add its twin to ``site/policy-eval.js``, then the conformance test
proves they agree. That friction is the point — a template the UI can build but
the engine evaluates differently would silently break the load-bearing
"preview == reality" promise.
"""

from __future__ import annotations

from typing import Any, Callable

from pherix.core.effects import Effect
from pherix.core.policy import Allow, Deny, PolicyContext, Verdict, refund_if_paid
from pherix.governance.spec import CapSpec, PolicySpec, RuleSpec


def arg_equals_denied(
    *,
    tool: str,
    arg: str,
    value: Any = None,
) -> Callable[[Effect, PolicyContext], Verdict]:
    """Args-only template: deny ``tool`` based on one of its arguments.

    Two modes:

    - ``value is None`` — deny whenever ``tool`` is called *with* ``arg`` present
      at all (e.g. "no ``force=...`` on ``delete``").
    - ``value`` given — deny only when ``args[arg] == value`` (e.g. "no
      ``tier='enterprise'`` updates").

    Pure function of the effect's args — no world-state read — so its verdict is
    identical at stage-time and commit-time.
    """

    def _rule(effect: Effect, ctx: PolicyContext) -> Verdict:
        if effect.tool != tool:
            return Allow()
        if arg not in effect.args:
            return Allow()
        if value is None:
            return Deny(
                f"arg_equals_denied: tool {tool!r} called with disallowed "
                f"arg {arg!r}"
            )
        if effect.args.get(arg) == value:
            return Deny(
                f"arg_equals_denied: tool {tool!r} has {arg!r}={value!r}, "
                f"which is denied"
            )
        return Allow()

    _rule.__name__ = f"arg_equals_denied({tool}.{arg})"
    return _rule


# name → factory. Keep the keys identical to the function names so the
# ``to_python`` export's import line (``from ...templates import <name>``) is a
# real symbol.
TEMPLATE_REGISTRY: dict[str, Callable[..., Callable[[Effect, PolicyContext], Verdict]]] = {
    "refund_if_paid": refund_if_paid,
    "arg_equals_denied": arg_equals_denied,
}


# -- the starter library -----------------------------------------------------


STARTER_TEMPLATES: list[PolicySpec] = [
    PolicySpec(
        name="spend-capped",
        description=(
            "Let the agent run, but cap how much it can spend and how many "
            "side-effecting calls it makes in one transaction. The safety net "
            "for a billing or notification agent."
        ),
        caps=[
            CapSpec(kind="sum", tool="charge", field="amount", max=1000),
            CapSpec(kind="count", tool="send_email", max=5),
        ],
        gate_irreversibles=True,
    ),
    PolicySpec(
        name="read-only",
        description=(
            "Allow only read tools; everything else is denied. The strictest "
            "starting point — swap in your own read tool names. Good for a "
            "research / analysis agent that must never mutate."
        ),
        allow=["read_file", "sql_select", "http_get", "list_dir"],
        gate_irreversibles=True,
    ),
    PolicySpec(
        name="approve-irreversibles",
        description=(
            "Reversible work runs freely; anything that can't be rolled back "
            "(no compensator) blocks at commit for a human approval. The "
            "default posture for an agent acting on production."
        ),
        gate_irreversibles=True,
    ),
    PolicySpec(
        name="refund-guarded",
        description=(
            "A refund tool may only fire if the order is 'paid' right now — "
            "checked live against world-state at both stage-time and "
            "commit-time, so a concurrent state change between the two flips "
            "the verdict. The canonical TOCTOU-safe rule."
        ),
        rules=[
            RuleSpec(
                template="refund_if_paid",
                params={
                    "tool": "refund_order",
                    "table": "orders",
                    "id_arg": "order_id",
                    "pk_column": "id",
                    "status_column": "status",
                    "paid_value": "paid",
                    "resource": "sql",
                },
            ),
        ],
        gate_irreversibles=True,
    ),
]


__all__ = [
    "STARTER_TEMPLATES",
    "TEMPLATE_REGISTRY",
    "arg_equals_denied",
    "refund_if_paid",
]
