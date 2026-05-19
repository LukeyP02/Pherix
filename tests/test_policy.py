import pytest

from pherix.core.effects import Effect
from pherix.core.policy import (
    Allow,
    Cap,
    Deny,
    Policy,
    PolicyContext,
    PolicyRule,
    PolicyViolation,
)


def _effect(tool: str, args: dict | None = None, *, index: int = 0) -> Effect:
    """Build a free-standing Effect for policy unit tests — no runtime needed."""
    return Effect(
        txn_id="txn-test",
        index=index,
        tool=tool,
        args=args or {},
        resource="any",
        reversible=True,
    )


def _ctx(*, where="stage", journal=None) -> PolicyContext:
    return PolicyContext(journal=journal or [], where=where)


# --- Slice 1 backwards-compat ----------------------------------------------


def test_default_policy_permits_everything():
    p = Policy()
    p.check("anything")
    assert p.permits("anything") is True


def test_allow_list_restricts_to_listed_tools():
    p = Policy(allow={"insert_user"})
    p.check("insert_user")
    with pytest.raises(PolicyViolation):
        p.check("delete_user")
    assert p.permits("delete_user") is False


def test_deny_list_blocks_listed_tools():
    p = Policy(deny={"drop_table"})
    p.check("insert_user")
    with pytest.raises(PolicyViolation):
        p.check("drop_table")


def test_deny_wins_over_allow():
    p = Policy(allow={"risky"}, deny={"risky"})
    with pytest.raises(PolicyViolation):
        p.check("risky")


def test_violation_carries_tool_name():
    p = Policy(allow=set())
    with pytest.raises(PolicyViolation) as exc:
        p.check("insert_user")
    assert exc.value.tool == "insert_user"


def test_allow_all_factory():
    assert Policy.allow_all().permits("whatever") is True


# --- Slice 6 D2: rules as Python callables ---------------------------------


def test_policy_rule_decorator_registers_a_callable():
    policy = Policy.allow_all()

    @policy.rule
    def block_enterprise(effect, ctx):
        if effect.args.get("tier") == "enterprise":
            return Deny("enterprise tier off-limits")
        return Allow()

    assert len(policy.rules) == 1
    assert policy.rules[0].name == "block_enterprise"

    # Allow path leaves the decorated fn directly callable, un-wrapped.
    assert block_enterprise(_effect("x", {"tier": "basic"}), _ctx()) == Allow()


def test_evaluate_allows_when_no_rule_denies():
    policy = Policy.allow_all()

    @policy.rule
    def allow_all(effect, ctx):
        return Allow()

    policy.evaluate(_effect("read_user"), _ctx())  # no raise


def test_evaluate_denies_when_rule_returns_deny():
    policy = Policy.allow_all()

    @policy.rule
    def no_enterprise(effect, ctx):
        if effect.tool == "update_user" and effect.args.get("tier") == "enterprise":
            return Deny("enterprise tier off-limits")
        return Allow()

    # Args-aware: same tool, different args, different verdict.
    policy.evaluate(_effect("update_user", {"tier": "basic"}), _ctx())
    with pytest.raises(PolicyViolation, match="enterprise tier off-limits") as exc:
        policy.evaluate(_effect("update_user", {"tier": "enterprise"}), _ctx())
    assert exc.value.where == "stage"
    assert exc.value.rule.name == "no_enterprise"
    assert exc.value.reason == "enterprise tier off-limits"


def test_with_rules_factory_composes_a_policy():
    def deny_writes(effect, ctx):
        if effect.tool.startswith("write_"):
            return Deny(f"{effect.tool} forbidden")
        return Allow()

    policy = Policy.with_rules(rules=[deny_writes], deny={"drop_table"})
    policy.evaluate(_effect("read_user"), _ctx())
    with pytest.raises(PolicyViolation):
        policy.evaluate(_effect("write_user"), _ctx())
    with pytest.raises(PolicyViolation, match="deny-listed"):
        policy.evaluate(_effect("drop_table"), _ctx())


# --- Slice 6 D4: spend caps -------------------------------------------------


def test_cap_count_allows_until_limit_then_denies():
    policy = Policy.with_rules(caps=[Cap.count(tool="ping", max=2)])
    ctx = _ctx()
    policy.evaluate(_effect("ping", index=0), ctx)
    policy.evaluate(_effect("ping", index=1), ctx)
    with pytest.raises(PolicyViolation) as exc:
        policy.evaluate(_effect("ping", index=2), ctx)
    assert exc.value.rule.name.startswith("Cap.count")
    assert "max=2" in exc.value.reason


def test_cap_count_ignores_other_tools():
    policy = Policy.with_rules(caps=[Cap.count(tool="ping", max=1)])
    ctx = _ctx()
    policy.evaluate(_effect("ping", index=0), ctx)
    policy.evaluate(_effect("other", index=1), ctx)
    policy.evaluate(_effect("other", index=2), ctx)
    with pytest.raises(PolicyViolation):
        policy.evaluate(_effect("ping", index=3), ctx)


def test_cap_sum_denies_when_cumulative_exceeds_max():
    policy = Policy.with_rules(
        caps=[
            Cap.sum(tool="charge", via=lambda args: args["amount"], max=50),
        ]
    )
    ctx = _ctx()
    policy.evaluate(_effect("charge", {"amount": 20}, index=0), ctx)
    policy.evaluate(_effect("charge", {"amount": 25}, index=1), ctx)
    with pytest.raises(PolicyViolation, match="sum cap") as exc:
        policy.evaluate(_effect("charge", {"amount": 10}, index=2), ctx)
    assert exc.value.rule.name.startswith("Cap.sum")


def test_two_caps_compose_independently():
    policy = Policy.with_rules(
        caps=[
            Cap.count(tool="ping", max=2),
            Cap.sum(tool="charge", via=lambda args: args["amount"], max=50),
        ]
    )
    ctx = _ctx()
    policy.evaluate(_effect("ping", index=0), ctx)
    policy.evaluate(_effect("charge", {"amount": 40}, index=1), ctx)
    policy.evaluate(_effect("ping", index=2), ctx)
    # ping cap hit
    with pytest.raises(PolicyViolation, match="count cap"):
        policy.evaluate(_effect("ping", index=3), ctx)


# --- Slice 6 D5: enriched PolicyViolation ----------------------------------


def test_policy_violation_carries_where_rule_reason_effect_index():
    policy = Policy.allow_all()

    @policy.rule
    def always_deny(effect, ctx):
        return Deny("nope")

    with pytest.raises(PolicyViolation) as exc:
        policy.evaluate(_effect("anything", index=7), _ctx(where="commit"), where="commit")
    assert exc.value.where == "commit"
    assert exc.value.rule.name == "always_deny"
    assert exc.value.reason == "nope"
    assert exc.value.effect_index == 7


def test_policy_violation_effect_index_is_none_at_stage_time():
    policy = Policy.allow_all()

    @policy.rule
    def block(effect, ctx):
        return Deny("nope")

    with pytest.raises(PolicyViolation) as exc:
        policy.evaluate(_effect("anything", index=3), _ctx())
    assert exc.value.where == "stage"
    assert exc.value.effect_index is None


def test_policy_violation_is_backwards_compat_attribute_access():
    # Tests that the old (tool, reason) attribute reads still work for
    # callers who catch PolicyViolation and inspect either field.
    p = Policy(deny={"drop"})
    with pytest.raises(PolicyViolation) as exc:
        p.check("drop")
    assert exc.value.tool == "drop"
    assert exc.value.reason == "tool is deny-listed"


# --- Slice 6 D3: evaluate_journal (commit-time bracket) --------------------


class _FakeTxn:
    """Minimal stand-in for Transaction in evaluate_journal tests."""

    def __init__(self, effects):
        self.effects = effects


def test_evaluate_journal_resets_caps_and_re_folds():
    # Re-using a context that already accumulated cap totals at stage-time:
    # evaluate_journal must clear and re-walk from zero, otherwise the
    # commit-time fold double-counts.
    policy = Policy.with_rules(caps=[Cap.count(tool="ping", max=2)])
    effects = [_effect("ping", index=i) for i in range(2)]
    ctx = _ctx()
    for e in effects:
        policy.evaluate(e, ctx)
    # ctx now has running total 2 for the ping cap. evaluate_journal must
    # reset and re-walk — the same 2 effects must NOT trip the cap.
    txn = _FakeTxn(effects=effects)
    policy.evaluate_journal(txn, ctx)


def test_evaluate_journal_denies_at_commit_time_with_effect_index():
    policy = Policy.allow_all()

    @policy.rule
    def deny_third(effect, ctx):
        if effect.index == 2:
            return Deny("the third effect is unlucky")
        return Allow()

    txn = _FakeTxn(effects=[_effect("x", index=i) for i in range(3)])
    ctx = _ctx()
    with pytest.raises(PolicyViolation) as exc:
        policy.evaluate_journal(txn, ctx)
    assert exc.value.where == "commit"
    assert exc.value.effect_index == 2


# --- Slice 6.5 seam: ctx.read placeholder ----------------------------------


def test_ctx_read_raises_not_implemented_in_slice_6():
    ctx = _ctx()
    with pytest.raises(NotImplementedError, match="Slice 6.5"):
        ctx.read("sql", "users:42")


# --- Slice 6 rule + cap registered together --------------------------------


def test_rule_short_circuits_before_cap_when_first_in_order():
    # Rules fire BEFORE caps (per evaluate()'s fold order). A rule that
    # denies should short-circuit and the cap's running total should NOT
    # advance — the next effect sees an unchanged total.
    policy = Policy.allow_all()

    @policy.rule
    def block_bigs(effect, ctx):
        if effect.args.get("amount", 0) > 30:
            return Deny("too big")
        return Allow()

    policy.add_cap(Cap.sum(tool="charge", via=lambda a: a["amount"], max=100))
    ctx = _ctx()
    policy.evaluate(_effect("charge", {"amount": 20}), ctx)
    with pytest.raises(PolicyViolation, match="too big"):
        policy.evaluate(_effect("charge", {"amount": 50}), ctx)
    # Cap running total is still 20 (the denied effect didn't accumulate).
    cap = policy.caps[0]
    assert ctx.cap_running(cap) == 20
