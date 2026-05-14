import pytest

from pherix.core.policy import Policy, PolicyViolation


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
