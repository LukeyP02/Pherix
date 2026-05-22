"""MemoryAdapter — governed agent memory as adapter + policy (the Phase-3 pillar).

These tests prove the north-star claim that governed memory is **not a new
axis**: memory drops onto the existing :class:`ResourceAdapter` protocol and the
existing policy machinery with *no engine surgery*. They exercise the four
deliverables of the spec — journalled effects, rollback with the txn,
memory-specific policy (PII deny + growth cap), and the audit/endpoint story —
plus the convergent-generalisation bonus: world-state reads over memory through
the unchanged ``sql_reader`` mediator, and MCP-gateway exposure with no new
front-end code. Fully offline.
"""

import json
import sqlite3

import pytest

from pherix.core.adapters.memory import MemoryAdapter
from pherix.core.audit import AuditJournal
from pherix.core.dry_run import dry_run
from pherix.core.effects import EffectStatus
from pherix.core.memory import (
    memory_byte_cap,
    no_pii,
    register_memory_tools,
)
from pherix.core.policy import Allow, Cap, Deny, Policy, PolicyViolation
from pherix.core.runtime import agent_txn
from pherix.frontends.proxy import InProcessMCPClient, PherixGateway


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    yield c
    c.close()


@pytest.fixture
def adapter(conn):
    return MemoryAdapter(conn)


@pytest.fixture
def adapters(adapter):
    return {"memory": adapter}


@pytest.fixture
def mem_tools():
    """Register the standard remember/recall/forget tools for one test."""
    return register_memory_tools()


def _stored(conn, namespace="default"):
    return {
        k: v
        for k, v in conn.execute(
            "SELECT mem_key, value FROM _pherix_memory WHERE namespace = ?",
            (namespace,),
        ).fetchall()
    }


# -- 1. memory effects journal ----------------------------------------------


def test_remember_recall_forget_land_in_the_journal(adapters, mem_tools):
    audit = AuditJournal.in_memory()
    with agent_txn(adapters, audit=audit) as txn:
        mem_tools.remember(key="city", value="London")
        assert mem_tools.recall(key="city") == "London"
        mem_tools.forget(key="city")

    effects = audit.get_effects(txn.txn_id)
    assert [e["tool"] for e in effects] == ["remember", "recall", "forget"]
    assert all(e["resource"] == "memory" for e in effects)
    # Every memory effect is reversible (savepoint-backed) and APPLIED on commit.
    assert all(e["reversible"] == 1 for e in effects)
    assert all(e["status"] == EffectStatus.APPLIED.name for e in effects)


# -- 2. rollback with the transaction ---------------------------------------


def test_remember_rolls_back_with_the_txn(conn, adapters, mem_tools):
    with agent_txn(adapters) as txn:
        mem_tools.remember(key="city", value="London")
        txn.rollback()
    # A rolled-back remember simply never happened.
    assert _stored(conn) == {}


def test_exception_in_block_rolls_back_memory(conn, adapters, mem_tools):
    with pytest.raises(RuntimeError, match="boom"):
        with agent_txn(adapters):
            mem_tools.remember(key="city", value="London")
            raise RuntimeError("boom")
    assert _stored(conn) == {}


# -- 3 & 5. recall sees committed memory, not a rolled-back write -----------


def test_recall_sees_committed_memory(conn, adapters, mem_tools):
    with agent_txn(adapters):
        mem_tools.remember(key="city", value="London")
    # New transaction, same store: the committed write is visible.
    with agent_txn(adapters):
        assert mem_tools.recall(key="city") == "London"


def test_recall_does_not_see_a_rolled_back_write(conn, adapters, mem_tools):
    with agent_txn(adapters) as txn:
        mem_tools.remember(key="city", value="Paris")
        txn.rollback()
    with agent_txn(adapters):
        assert mem_tools.recall(key="city") is None


def test_forget_rolls_back_too(conn, adapters, mem_tools):
    with agent_txn(adapters):
        mem_tools.remember(key="city", value="London")
    with agent_txn(adapters) as txn:
        mem_tools.forget(key="city")
        txn.rollback()
    # The forget was undone — the value is back.
    with agent_txn(adapters):
        assert mem_tools.recall(key="city") == "London"


# -- recall is read-only by construction ------------------------------------


def test_recall_records_a_read_key_and_no_write_key(adapters, mem_tools):
    audit = AuditJournal.in_memory()
    with agent_txn(adapters, audit=audit) as txn:
        mem_tools.remember(key="city", value="London")
        mem_tools.recall(key="city")
    effects = {e["tool"]: e for e in audit.get_effects(txn.txn_id)}
    recall = effects["recall"]
    assert json.loads(recall["read_keys"]) != []
    assert json.loads(recall["write_keys"]) == []
    # The write tool, by contrast, records a write key.
    assert json.loads(effects["remember"]["write_keys"]) != []


# -- 4. memory-specific policy: PII deny ------------------------------------


def test_pii_rule_denies_a_remember(conn, adapters, mem_tools):
    policy = Policy.with_rules(rules=[no_pii()])
    with pytest.raises(PolicyViolation, match="email pattern"):
        with agent_txn(adapters, policy=policy):
            mem_tools.remember(key="note", value="contact me at a@b.com")
    # Nothing PII-bearing reached the store.
    assert _stored(conn) == {}


def test_pii_rule_allows_clean_content_and_never_blocks_recall(
    conn, adapters, mem_tools
):
    policy = Policy.with_rules(rules=[no_pii()])
    with agent_txn(adapters, policy=policy):
        mem_tools.remember(key="note", value="the meeting is at noon")
        # recall carries no new content — the PII rule is a no-op for it.
        assert mem_tools.recall(key="note") == "the meeting is at noon"
    assert _stored(conn) == {"note": "the meeting is at noon"}


def test_pii_rule_catches_pii_nested_in_a_dict_value(conn, adapters, mem_tools):
    policy = Policy.with_rules(rules=[no_pii()])
    with pytest.raises(PolicyViolation, match="ssn pattern"):
        with agent_txn(adapters, policy=policy):
            mem_tools.remember(key="rec", value={"ssn": "123-45-6789"})
    assert _stored(conn) == {}


# -- 4. memory-specific policy: growth cap ----------------------------------


def test_byte_cap_denies_growth_beyond_budget(conn, adapters, mem_tools):
    # Budget of 10 bytes; the first small value fits, the second overflows.
    policy = Policy.with_rules(caps=[memory_byte_cap(max_bytes=10)])
    with pytest.raises(PolicyViolation, match="sum cap"):
        with agent_txn(adapters, policy=policy):
            mem_tools.remember(key="a", value="hello")  # 5 bytes, ok
            mem_tools.remember(key="b", value="world!")  # +6 = 11 > 10
    # The whole txn rolled back on the denial — neither write persisted.
    assert _stored(conn) == {}


def test_count_cap_covers_memory_for_free(conn, adapters, mem_tools):
    # A growth cap can also be a plain count cap — no memory-specific primitive.
    policy = Policy.with_rules(caps=[Cap.count(tool="remember", max=1)])
    with pytest.raises(PolicyViolation, match="count cap"):
        with agent_txn(adapters, policy=policy):
            mem_tools.remember(key="a", value="x")
            mem_tools.remember(key="b", value="y")
    assert _stored(conn) == {}


# -- world-state policy over memory (the #7 bonus, no engine surgery) -------


def test_world_state_rule_reads_live_memory_through_sql_reader(
    conn, adapters, mem_tools
):
    """A rule that refuses to remember a key marked locked in memory.

    It reads the *live* committed lock marker via ``ctx.read("memory", ...)`` —
    the same world-state mediator (`sql_reader`) the SQL adapter uses, reaching
    memory through the unchanged runtime because the adapter exposes ``.conn``.
    The marker is a key the txn never writes, so the commit-time re-evaluation
    reads stable committed state (no read-your-writes artefact) — proof that the
    #7 world-state axis covers memory with no engine surgery.
    """

    def no_locked_writes(effect, ctx):
        if effect.tool != "remember":
            return Allow()
        key = effect.args["key"]
        lock = f"__lock__{key}"
        live = ctx.read("memory", ("_pherix_memory", "mem_key", lock, "value"))
        if live is not None:
            return Deny(f"key {key!r} is locked in memory; refusing to write")
        return Allow()

    # Seed a committed lock marker for 'city' (no policy on the seeding txn).
    with agent_txn(adapters):
        mem_tools.remember(key="__lock__city", value="1")

    policy = Policy.with_rules(rules=[no_locked_writes])
    # Writing the locked key is denied — the rule read the live marker.
    with pytest.raises(PolicyViolation, match="is locked in memory"):
        with agent_txn(adapters, policy=policy):
            mem_tools.remember(key="city", value="Paris")
    # An unlocked key writes fine under the same policy.
    with agent_txn(adapters, policy=policy):
        mem_tools.remember(key="country", value="France")
    with agent_txn(adapters):
        assert mem_tools.recall(key="country") == "France"
        assert mem_tools.recall(key="city") is None


# -- 6. durable round-trip across adapters (offline) ------------------------


def test_committed_memory_survives_a_fresh_adapter(tmp_path, mem_tools):
    db = str(tmp_path / "memory.db")

    conn1 = sqlite3.connect(db, isolation_level=None)
    with agent_txn({"memory": MemoryAdapter(conn1)}):
        mem_tools.remember(key="city", value="Berlin")
    conn1.close()

    # A brand-new connection + adapter on the same file recalls the value:
    # durability is the SQLite file persisting committed state across runs.
    conn2 = sqlite3.connect(db, isolation_level=None)
    with agent_txn({"memory": MemoryAdapter(conn2)}):
        assert mem_tools.recall(key="city") == "Berlin"
    conn2.close()


def test_rolled_back_write_is_not_durable(tmp_path, mem_tools):
    db = str(tmp_path / "memory.db")

    conn1 = sqlite3.connect(db, isolation_level=None)
    with agent_txn({"memory": MemoryAdapter(conn1)}) as txn:
        mem_tools.remember(key="city", value="Berlin")
        txn.rollback()
    conn1.close()

    # The rolled-back write left nothing durable on disk.
    conn2 = sqlite3.connect(db, isolation_level=None)
    with agent_txn({"memory": MemoryAdapter(conn2)}):
        assert mem_tools.recall(key="city") is None
    conn2.close()


# -- namespacing: two agents' memories don't collide ------------------------


def test_namespaces_isolate_two_agents(conn, mem_tools):
    a = MemoryAdapter(conn, namespace="agent-a")
    b = MemoryAdapter(conn, namespace="agent-b")
    with agent_txn({"memory": a}):
        mem_tools.remember(key="k", value="from-a")
    with agent_txn({"memory": b}):
        mem_tools.remember(key="k", value="from-b")
        assert mem_tools.recall(key="k") == "from-b"
    with agent_txn({"memory": a}):
        assert mem_tools.recall(key="k") == "from-a"


# -- 3 (endpoint) + 4 (audit): memory over the MCP gateway, no new code -----


def test_memory_tools_are_exposed_over_the_mcp_gateway(conn):
    """The interception axis covers memory for free: registered memory tools
    appear in ``tools/list`` and run through ``tools/call`` with no new
    front-end code — the gateway just enumerates the same registry."""
    register_memory_tools()
    adapters = {"memory": MemoryAdapter(conn)}
    audit = AuditJournal.in_memory()
    gw = PherixGateway(
        adapters=adapters, default_policy=Policy.allow_all(), audit=audit
    )
    client = InProcessMCPClient(gw)
    client.initialize("claude-code")

    names = [t["name"] for t in client.tool_descriptors()]
    assert {"remember", "recall", "forget"} <= set(names)

    # remember over the wire, then recall over the wire — round-trips.
    resp = client.call_tool("remember", {"key": "city", "value": "Tokyo"})
    out = client.structured_of(resp)
    assert out["committed"] is True

    resp = client.call_tool("recall", {"key": "city"})
    assert client.structured_of(resp)["result"] == "Tokyo"

    # 4. The audit journal shows the full memory story — committed memory txns.
    rows = audit._conn.execute(
        "SELECT tool, resource, status FROM effects WHERE tool IN "
        "('remember','recall') ORDER BY idx"
    ).fetchall()
    assert any(r[0] == "remember" and r[1] == "memory" for r in rows)


# -- audit/dry-run structural diff over memory ------------------------------


def test_dry_run_reports_a_memory_state_diff(conn, adapters, mem_tools):
    with dry_run(adapters) as ctx:
        mem_tools.remember(key="city", value="Oslo")
    # The dry-run discarded the write but still reports the structural delta.
    assert ctx.result.state_diff["memory"]["keys_added"] == ["city"]
    assert _stored(conn) == {}
