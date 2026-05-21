"""Preview against seeded journals — the explainer's verdicts are correct.

Covers each disposition (allow / deny / cap / gate) and the world-state path
(``refund_if_paid`` reading a sample world map), including the TOCTOU divergence
that is the whole point of world-state rules.
"""

from __future__ import annotations

from pherix.governance.preview import EffectScenario, preview
from pherix.governance.spec import CapSpec, PolicySpec, RuleSpec


# -- allow / deny ------------------------------------------------------------


def test_allow_list_denies_unlisted_tool():
    spec = PolicySpec(name="readonly", allow=["read_file"])
    result = preview(
        spec,
        [
            EffectScenario(tool="read_file", args={}),
            EffectScenario(tool="write_file", args={}),
        ],
    )
    assert [r.disposition for r in result.rows] == ["allow", "deny"]
    assert "allow-list" in result.rows[1].reasons[0]


def test_deny_list_denies():
    spec = PolicySpec(name="d", deny=["drop_table"])
    result = preview(spec, [EffectScenario(tool="drop_table", args={})])
    assert result.rows[0].disposition == "deny"


# -- caps --------------------------------------------------------------------


def test_count_cap_trips_on_the_third_call():
    spec = PolicySpec(name="c", caps=[CapSpec(kind="count", tool="send", max=2)])
    result = preview(
        spec,
        [EffectScenario(tool="send", args={}) for _ in range(3)],
    )
    assert [r.disposition for r in result.rows] == ["allow", "allow", "cap"]


def test_sum_cap_accumulates_across_journal():
    spec = PolicySpec(
        name="c", caps=[CapSpec(kind="sum", tool="charge", field="amount", max=100)]
    )
    result = preview(
        spec,
        [
            EffectScenario(tool="charge", args={"amount": 60}),
            EffectScenario(tool="charge", args={"amount": 60}),  # 120 > 100
        ],
    )
    assert [r.disposition for r in result.rows] == ["allow", "cap"]


def test_sum_cap_missing_field_contributes_zero():
    spec = PolicySpec(
        name="c", caps=[CapSpec(kind="sum", tool="charge", field="amount", max=100)]
    )
    result = preview(spec, [EffectScenario(tool="charge", args={})])
    assert result.rows[0].disposition == "allow"


# -- gate --------------------------------------------------------------------


def test_irreversible_without_compensator_gates():
    spec = PolicySpec(name="g", gate_irreversibles=True)
    result = preview(
        spec,
        [EffectScenario(tool="send_webhook", args={}, reversible=False)],
    )
    assert result.rows[0].disposition == "gate"


def test_irreversible_with_compensator_does_not_gate():
    spec = PolicySpec(name="g", gate_irreversibles=True)
    result = preview(
        spec,
        [
            EffectScenario(
                tool="charge", args={}, reversible=False, compensator="refund"
            )
        ],
    )
    assert result.rows[0].disposition == "allow"


def test_gate_off_lets_irreversible_through():
    spec = PolicySpec(name="g", gate_irreversibles=False)
    result = preview(
        spec,
        [EffectScenario(tool="send_webhook", args={}, reversible=False)],
    )
    assert result.rows[0].disposition == "allow"


# -- world-state (refund_if_paid) --------------------------------------------


def _refund_spec() -> PolicySpec:
    return PolicySpec(
        name="refund",
        rules=[
            RuleSpec(
                template="refund_if_paid",
                params={"tool": "refund_order", "table": "orders"},
            )
        ],
    )


def test_refund_allowed_when_order_paid():
    result = preview(
        _refund_spec(),
        [EffectScenario(tool="refund_order", args={"order_id": 42})],
        world=[
            {"resource": "sql", "key": ["orders", "id", 42, "status"], "value": "paid"}
        ],
    )
    assert result.rows[0].disposition == "allow"


def test_refund_denied_when_order_not_paid():
    result = preview(
        _refund_spec(),
        [EffectScenario(tool="refund_order", args={"order_id": 42})],
        world=[
            {
                "resource": "sql",
                "key": ["orders", "id", 42, "status"],
                "value": "refunded",
            }
        ],
    )
    assert result.rows[0].disposition == "deny"
    assert "refunded" in result.rows[0].reasons[0]


def test_refund_denied_when_order_absent_reads_none():
    result = preview(
        _refund_spec(),
        [EffectScenario(tool="refund_order", args={"order_id": 99})],
        world=[],
    )
    assert result.rows[0].disposition == "deny"


def test_world_state_divergence_between_two_worlds():
    # Same spec, same effect, two worlds → opposite verdicts. This is the
    # TOCTOU property: the predicate P(effect, world) flips when only the
    # world moves. (In the runtime, "two worlds" = stage-time vs commit-time.)
    spec = _refund_spec()
    effect = [EffectScenario(tool="refund_order", args={"order_id": 7})]
    paid = preview(
        spec,
        effect,
        world=[{"resource": "sql", "key": ["orders", "id", 7, "status"], "value": "paid"}],
    )
    flipped = preview(
        spec,
        effect,
        world=[
            {"resource": "sql", "key": ["orders", "id", 7, "status"], "value": "void"}
        ],
    )
    assert paid.rows[0].disposition == "allow"
    assert flipped.rows[0].disposition == "deny"


# -- aggregate signals -------------------------------------------------------


def test_counts_and_is_clean():
    spec = PolicySpec(name="x", deny=["bad"])
    result = preview(
        spec,
        [
            EffectScenario(tool="good", args={}),
            EffectScenario(tool="bad", args={}),
        ],
    )
    assert result.counts == {"allow": 1, "deny": 1, "cap": 0, "gate": 0}
    assert result.is_clean is False


def test_is_clean_true_when_all_allow():
    result = preview(PolicySpec(name="x"), [EffectScenario(tool="anything", args={})])
    assert result.is_clean is True
