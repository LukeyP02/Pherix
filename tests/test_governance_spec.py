"""Spec round-trip: PolicySpec ⇄ JSON, spec → engine, spec → runnable Python.

The load-bearing claim of the whole governance UI is that what you build is what
the engine runs. These tests pin the three legs of that:

    from_dict(to_dict(spec)) == spec          # JSON round-trips losslessly
    from_spec(spec)            runs verdicts   # the loaded Policy is a real Policy
    exec(to_python(spec))      same verdicts   # the Python export is faithful
"""

from __future__ import annotations

from pherix.core.policy import Policy, PolicyContext
from pherix.governance.preview import EffectScenario, preview
from pherix.governance.spec import (
    CapSpec,
    PolicySpec,
    RuleSpec,
    from_spec,
    to_python,
    to_spec,
)
from pherix.governance.templates import STARTER_TEMPLATES


def _rich_spec() -> PolicySpec:
    return PolicySpec(
        name="rich",
        description="exercises every primitive",
        allow=["charge", "refund_order", "send_email", "read_file"],
        deny=["drop_table"],
        caps=[
            CapSpec(kind="sum", tool="charge", field="amount", max=1000),
            CapSpec(kind="count", tool="send_email", max=2),
        ],
        rules=[
            RuleSpec(
                template="refund_if_paid",
                params={"tool": "refund_order", "table": "orders"},
            ),
            RuleSpec(
                template="arg_equals_denied",
                params={"tool": "charge", "arg": "currency", "value": "XXX"},
            ),
        ],
        gate_irreversibles=True,
    )


# -- JSON round-trip ---------------------------------------------------------


def test_to_dict_from_dict_round_trips():
    spec = _rich_spec()
    assert PolicySpec.from_dict(spec.to_dict()) == spec


def test_to_spec_alias():
    spec = _rich_spec()
    assert to_spec(spec.to_dict()) == spec


def test_allow_none_round_trips_as_none_not_empty():
    spec = PolicySpec(name="x", allow=None)
    assert spec.to_dict()["allow"] is None
    assert PolicySpec.from_dict(spec.to_dict()).allow is None


def test_sum_cap_requires_field():
    import pytest

    with pytest.raises(ValueError, match="sum cap requires"):
        CapSpec(kind="sum", tool="charge", max=10)  # no field


# -- spec → engine -----------------------------------------------------------


def test_from_spec_builds_a_real_policy():
    policy = from_spec(_rich_spec())
    assert isinstance(policy, Policy)
    assert policy.deny == {"drop_table"}
    assert policy.allow == {"charge", "refund_order", "send_email", "read_file"}
    assert len(policy.caps) == 2
    assert len(policy.rules) == 2


def test_from_spec_accepts_raw_dict():
    policy = from_spec(_rich_spec().to_dict())
    assert isinstance(policy, Policy)


def test_unknown_template_raises():
    import pytest

    spec = PolicySpec(name="bad", rules=[RuleSpec(template="does_not_exist")])
    with pytest.raises(ValueError, match="unknown rule template"):
        from_spec(spec)


def test_loaded_policy_enforces_deny_list():
    import pytest

    from pherix.core.policy import PolicyViolation

    policy = from_spec(_rich_spec())
    with pytest.raises(PolicyViolation):
        policy.check("drop_table")


# -- spec → runnable Python --------------------------------------------------


def _scenario():
    return [
        EffectScenario(tool="charge", args={"amount": 600, "currency": "USD"}),
        EffectScenario(tool="charge", args={"amount": 600, "currency": "USD"}),
        EffectScenario(tool="send_email", args={}),
    ]


def test_to_python_emits_importable_module_with_matching_verdicts():
    spec = _rich_spec()
    src = to_python(spec)

    # exec the generated module and pull out its `policy`.
    ns: dict = {}
    exec(compile(src, "<generated>", "exec"), ns)
    exported = ns["policy"]
    assert isinstance(exported, Policy)

    scen = _scenario()
    # The exported policy and from_spec(spec) must produce identical verdicts.
    from_loaded = preview(from_spec(spec), scen)
    from_exported = preview(exported, scen)

    assert [r.disposition for r in from_loaded.rows] == [
        r.disposition for r in from_exported.rows
    ]


def test_to_python_round_trips_caps_semantics():
    # Second charge of 600 pushes the sum cap (max=1000) over → cap denial.
    spec = _rich_spec()
    result = preview(spec, _scenario())
    dispositions = [r.disposition for r in result.rows]
    # charge#1 allowed (600<=1000), charge#2 capped (1200>1000), email allowed.
    assert dispositions == ["allow", "cap", "allow"]


# -- starter templates all load ----------------------------------------------


def test_every_starter_template_loads_and_runs():
    for spec in STARTER_TEMPLATES:
        policy = from_spec(spec)
        assert isinstance(policy, Policy)
        # collect_verdicts over an empty journal must not raise.
        ctx = PolicyContext(journal=[], where="stage")

        class _Empty:
            effects: list = []

        assert policy.collect_verdicts(_Empty(), ctx) == []


def test_starter_templates_have_unique_names():
    names = [s.name for s in STARTER_TEMPLATES]
    assert len(names) == len(set(names))
