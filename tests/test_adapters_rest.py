"""RESTAdapter — irreversible REST/GraphQL transport adapter + harness.

All offline: the transport is an injectable fake that records calls and never
touches the network.
"""

from __future__ import annotations

import pytest

from pherix.core.adapters.base import (
    ResourceAdapter,
    SnapshotHandle,
    TransactionalResourceAdapter,
)
from pherix.core.adapters.http import IrreversibleAdapterError
from pherix.core.adapters.rest import RESTAdapter, graphql_tool, rest_tool
from pherix.core.effects import Effect, EffectStatus
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import tool


class FakeTransport:
    """Records every (method, url, kwargs) and returns a canned response."""

    def __init__(self, response=None, raises: Exception | None = None):
        self.calls: list[dict] = []
        self.response = response if response is not None else {"status": 200}
        self.raises = raises

    def __call__(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, "kwargs": kw})
        if self.raises is not None:
            raise self.raises
        return self.response


def _effect(**overrides):
    base = dict(
        txn_id="txn-1",
        index=0,
        tool="create_user",
        args={"json": {"name": "ada"}},
        resource="rest",
        reversible=False,
    )
    base.update(overrides)
    return Effect(**base)


# --- protocol / honesty -------------------------------------------------------


def test_conforms_to_resource_adapter_protocol():
    assert isinstance(RESTAdapter(), ResourceAdapter)


def test_does_not_conform_to_transactional_sub_protocol():
    assert not isinstance(RESTAdapter(), TransactionalResourceAdapter)


def test_supports_rollback_is_false():
    assert RESTAdapter().supports_rollback() is False


def test_snapshot_raises_irreversible():
    with pytest.raises(IrreversibleAdapterError):
        RESTAdapter().snapshot(_effect())


def test_restore_raises_irreversible():
    with pytest.raises(IrreversibleAdapterError):
        RESTAdapter().restore(SnapshotHandle(resource="rest", effect_index=0))


def test_read_and_write_version_raise_irreversible():
    with pytest.raises(IrreversibleAdapterError):
        RESTAdapter().read_version(("rest", "k"))
    with pytest.raises(IrreversibleAdapterError):
        RESTAdapter().write_version(("rest", "k"))


def test_apply_invokes_tool_with_bound_args_no_handle():
    seen: list[dict] = []

    def fake_tool(json):
        seen.append(json)
        return {"status": 201}

    result = RESTAdapter().apply(_effect(), fake_tool)
    assert seen == [{"name": "ada"}]
    assert result == {"status": 201}


# --- harness: rest_tool -------------------------------------------------------


def test_rest_tool_outside_txn_passes_through_to_transport():
    transport = FakeTransport(response={"status": 201, "body": {"id": 1}})
    create = rest_tool(
        "create_user", method="POST", url="https://api/users", transport=transport
    )
    # Outside agent_txn the @tool wrapper is a transparent passthrough.
    out = create(json={"name": "ada"})
    assert out == {"status": 201, "body": {"id": 1}}
    assert transport.calls == [
        {"method": "POST", "url": "https://api/users", "kwargs": {"json": {"name": "ada"}}}
    ]


def test_rest_tool_does_not_fire_at_stage_time_fires_once_at_commit():
    transport = FakeTransport(response={"status": 201})
    create = rest_tool(
        "create_user", method="POST", url="https://api/users", transport=transport
    )
    with agent_txn({"rest": RESTAdapter()}) as txn:
        result = create(json={"name": "ada"})
        # Staged: nothing has hit the transport yet.
        assert transport.calls == []
        txn.approve_irreversible(result.effect_id)
    # Fired exactly once at commit.
    assert len(transport.calls) == 1
    assert txn.txn.effects[0].status is EffectStatus.APPLIED


def test_rest_tool_gates_without_compensator_or_approval():
    transport = FakeTransport()
    create = rest_tool(
        "create_user", method="POST", url="https://api/users", transport=transport
    )
    with pytest.raises(GateBlocked):
        with agent_txn({"rest": RESTAdapter()}):
            create(json={"name": "ada"})
            # No approve_irreversible, no compensator -> gate blocks at commit.
    # Gate-block unwinds without firing the irreversible.
    assert transport.calls == []


def test_rest_tool_transport_error_marks_failed_and_rolls_back():
    transport = FakeTransport(raises=RuntimeError("503 from SaaS"))
    create = rest_tool(
        "create_user", method="POST", url="https://api/users", transport=transport
    )
    with pytest.raises(RuntimeError, match="503 from SaaS"):
        with agent_txn({"rest": RESTAdapter()}) as txn:
            r = create(json={"name": "ada"})
            txn.approve_irreversible(r.effect_id)
    # The transport was reached (fired at commit) and raised; the effect is
    # recorded FAILED rather than APPLIED.
    assert len(transport.calls) == 1
    assert txn.txn.effects[0].status is EffectStatus.FAILED


# --- harness: compensated rollback (end-to-end) -------------------------------


def test_rest_tool_with_compensator_compensates_on_partial_failure():
    # End-to-end: a compensator-backed POST fires at commit, then a LATER
    # irreversible effect raises during the commit fold. The runtime walks the
    # fired prefix backward and invokes the compensator (the DELETE inverse).
    sends = FakeTransport(response={"status": 201, "body": {"id": "u_1"}})
    deletes = FakeTransport(response={"status": 204})

    # The compensator must be registered (any order) before stage-time.
    rest_tool("delete_user", method="DELETE", url="https://api/users/u_1", transport=deletes)
    create = rest_tool(
        "create_user",
        method="POST",
        url="https://api/users",
        transport=sends,
        compensator="delete_user",
    )
    boom = rest_tool(
        "send_welcome",
        method="POST",
        url="https://api/email",
        transport=FakeTransport(raises=RuntimeError("smtp down")),
    )

    with pytest.raises(RuntimeError, match="smtp down"):
        with agent_txn({"rest": RESTAdapter()}) as txn:
            create(json={"name": "ada"})
            r2 = boom(json={"to": "ada@x.io"})
            txn.approve_irreversible(r2.effect_id)

    # POST fired once on commit; DELETE fired once on the backward unwind.
    assert len(sends.calls) == 1
    assert len(deletes.calls) == 1
    assert deletes.calls[0]["method"] == "DELETE"
    assert txn.txn.effects[0].status is EffectStatus.COMPENSATED


def test_rest_compensator_receives_original_args():
    # The runtime hands the compensator args=effect.args (the ORIGINAL send's
    # journalled args), not the original's result. The rest_tool harness gives
    # its tool a VAR_KEYWORD (`**kwargs`) signature, so the agent's call-time
    # kwargs are journalled under a single `kwargs` key. A compensator written
    # against the harness shares that contract: it declares `**kwargs` and
    # reads `kwargs["kwargs"]` (or, equivalently, takes a `kwargs=` param).
    seen_comp_args: list[dict] = []

    @tool(resource="rest", reversible=False, injects_handle=False, name="undo_create")
    def undo_create(**kwargs):
        seen_comp_args.append(kwargs)
        return None

    create = rest_tool(
        "create_user",
        method="POST",
        url="https://api/users",
        transport=FakeTransport(),
        compensator="undo_create",
    )
    boom = rest_tool(
        "send_welcome",
        method="POST",
        url="https://api/email",
        transport=FakeTransport(raises=RuntimeError("smtp down")),
    )
    with pytest.raises(RuntimeError, match="smtp down"):
        with agent_txn({"rest": RESTAdapter()}) as txn:
            create(json={"name": "ada"}, headers={"x": "1"})
            r2 = boom(json={"to": "ada@x.io"})
            txn.approve_irreversible(r2.effect_id)

    # The original send's call-time kwargs, journalled under `kwargs`, reach
    # the compensator verbatim — same payload the POST went out with.
    assert seen_comp_args == [{"kwargs": {"json": {"name": "ada"}, "headers": {"x": "1"}}}]


# --- harness: graphql_tool ----------------------------------------------------


def test_graphql_tool_posts_query_and_variables():
    transport = FakeTransport(response={"status": 200, "body": {"data": {}}})
    mutation = "mutation($name:String!){ createUser(name:$name){ id } }"
    run = graphql_tool(
        "gql_create_user",
        url="https://api/graphql",
        query=mutation,
        transport=transport,
    )
    with agent_txn({"rest": RESTAdapter()}) as txn:
        r = run(variables={"name": "ada"})
        assert transport.calls == []  # staged, not fired
        txn.approve_irreversible(r.effect_id)

    assert transport.calls == [
        {
            "method": "POST",
            "url": "https://api/graphql",
            "kwargs": {"json": {"query": mutation, "variables": {"name": "ada"}}},
        }
    ]


def test_graphql_tool_defaults_variables_to_empty_dict():
    transport = FakeTransport()
    run = graphql_tool(
        "gql_ping", url="https://api/graphql", query="{ping}", transport=transport
    )
    with agent_txn({"rest": RESTAdapter()}) as txn:
        r = run()
        txn.approve_irreversible(r.effect_id)
    assert transport.calls[0]["kwargs"]["json"]["variables"] == {}


def test_graphql_mutation_compensated_by_sibling_mutation():
    fwd = FakeTransport(response={"status": 200})
    inv = FakeTransport(response={"status": 200})
    graphql_tool(
        "gql_uninvite",
        url="https://api/graphql",
        query="mutation($e:String!){ revokeInvite(email:$e) }",
        transport=inv,
    )
    invite = graphql_tool(
        "gql_invite",
        url="https://api/graphql",
        query="mutation($e:String!){ invite(email:$e) }",
        transport=fwd,
        compensator="gql_uninvite",
    )
    boom = graphql_tool(
        "gql_boom",
        url="https://api/graphql",
        query="mutation { willFail }",
        transport=FakeTransport(raises=RuntimeError("graphql 500")),
    )
    with pytest.raises(RuntimeError, match="graphql 500"):
        with agent_txn({"rest": RESTAdapter()}) as txn:
            invite(variables={"e": "ada@x.io"})
            r2 = boom()
            txn.approve_irreversible(r2.effect_id)
    assert len(fwd.calls) == 1
    assert len(inv.calls) == 1
    assert txn.txn.effects[0].status is EffectStatus.COMPENSATED
