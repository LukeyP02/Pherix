"""The trust-laws headline theorems — the three pillars as laws over the journal.

Pherix exists to make an enterprise trust its agent to take real actions. The
trust rests on three pillars, and this file *names* each as a deterministic
property over arbitrary effect sequences (Hypothesis-generated), then proves
it. These are the laws a buyer or auditor wants to read — "Pherix is provably
correct", not "trust our demo".

    1. BLAST RADIUS  — a mistake is contained, never catastrophic.
    2. AUDIT         — you can always prove what happened.
    3. OVERSIGHT     — a human stays on the irreversible (the wedge).

The engine is the thing under test and is FROZEN: a failing law here is a real
finding about the engine, never something to paper over.

Two of these theorems are the *new* claims this suite adds — the gaps the rest
of the law suite did not yet pin:

- ``test_audit_completeness_*`` — the journal holds a row for every effect
  executed (both paths) and, on the dry-run path, for every policy verdict
  evaluated. The commit path does NOT record verdicts in the frozen engine
  (verdict capture lives on the dry-run path only — see ``core/audit.py``'s
  ``record_verdicts`` docstring), so the theorem is scoped *per path* to the
  property the engine actually guarantees, empirically established by probe.

- ``test_oversight_gate_fuzz`` — over hundreds of random mixed sequences of
  reversible + irreversible(+/- compensator) + approve/no-approve actions, the
  oversight invariant holds for EVERY sequence: an irreversible effect fires at
  commit IFF it had a registered compensator OR was explicitly approved;
  otherwise commit raises ``GateBlocked``, nothing un-approved fired, and the
  world is untouched. This is the differentiated claim — broken if it can be.
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings

from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus, StagedResult
from pherix.core.policy import Allow, Policy
from pherix.core.runtime import GateBlocked, agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

from tests._laws import KV_KEYS, dump_kv, fresh_kv_conn, kv_programs, seed_programs
from tests._laws_gen import Action, ActionKind, mixed_programs

# Hypothesis re-runs each body many times against the function-scoped tool
# fixture; the tools are immutable across examples so the non-reset is
# intentional — exactly what this health-check suppression covers. Hundreds of
# examples on the oversight law per the task's "hammer hardest" mandate.
_LAW = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
_LAW_AUDIT = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ===========================================================================
# THEOREM 1 — BLAST RADIUS
#   rollback(apply(S, W)) == W : a reversible program folded forward then
#   backward restores the committed baseline byte-exactly, for any program S
#   over any world W. (The wider blast-radius surface — partial-failure
#   unwind, compensator left-inverse, missing-compensator STUCK — is federated
#   under @pytest.mark.blast_radius across the existing law suites; this is the
#   headline statement.)
# ===========================================================================


@pytest.fixture
def kv_tools():
    """The fixed reversible toolset, registered once for the whole function."""

    @tool(resource="sql")
    def kv_set(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        return v

    @tool(resource="sql")
    def kv_del(conn, k):
        conn.execute("DELETE FROM kv WHERE k = ?", (k,))

    return kv_set, kv_del


def _run_kv(tools, prog) -> None:
    kv_set, kv_del = tools
    for op in prog:
        if op.op == "set":
            kv_set(k=op.key, v=op.value)
        else:
            kv_del(k=op.key)


def _seed(conn: sqlite3.Connection, seed: dict[str, int]) -> None:
    for k, v in seed.items():
        conn.execute("INSERT INTO kv (k, v) VALUES (?, ?)", (k, v))


@pytest.mark.blast_radius
@given(seed=seed_programs(), prog=kv_programs())
@_LAW_AUDIT
def test_blast_radius_rollback_is_identity(kv_tools, seed, prog):
    """THEOREM (blast radius): ``rollback ∘ (apply*) = identity`` on world state.

    For any committed baseline and any reversible program, folding the journal
    forward (live applies) then backward (snapshot restores) lands the world
    byte-identical to where it started. A contained mistake leaves no trace.
    """
    conn = fresh_kv_conn()
    try:
        _seed(conn, seed)
        before = dump_kv(conn)
        with agent_txn({"sql": SQLiteAdapter(conn)}) as txn:
            _run_kv(kv_tools, prog)
            txn.rollback()
        assert dump_kv(conn) == before
        assert txn.txn.state is TxnState.ROLLED_BACK
        # Every applied effect was visited by the backward fold — none left
        # half-undone.
        assert all(
            e.status is EffectStatus.COMPENSATED for e in txn.txn.effects
        )
    finally:
        conn.close()


# ===========================================================================
# THEOREM 2 — AUDIT  (gap-fill: an explicit completeness theorem)
#   Completeness, scoped per path to what the FROZEN engine guarantees:
#     - commit path  : journal effect-count == executed-count; verdicts == 0
#                       (the frozen engine records no verdicts on commit — a
#                       real, reported gap; see module docstring).
#     - dry-run path : effect-completeness AND verdict-completeness — one
#                       verdict row per (effect × rule × phase), phases =
#                       {stage, commit}, so verdicts == 2 · N · R for N effects
#                       and R explicit rules under an all-Allow policy.
# ===========================================================================


@pytest.fixture
def kv_tool_only():
    """A single reversible kv tool — keeps the executed-effect count == len(prog)."""

    @tool(resource="sql")
    def kv_put(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        return v

    return kv_put


@pytest.mark.audit
@given(prog=kv_programs(min_size=0, max_size=12))
@_LAW_AUDIT
def test_audit_completeness_commit_path(kv_tool_only, prog):
    """THEOREM (audit completeness, commit path): every executed effect is recorded.

    Over fuzzed reversible programs committed for real, the audit journal holds
    exactly one row per tool call, each APPLIED, in index order. The journal
    count equals the executed count — nothing is silently missing.

    FINDING (frozen engine): the commit path records **no** policy verdicts —
    verdict capture is implemented only on the dry-run path (``core/audit.py``
    ``record_verdicts`` docstring). So verdict-completeness is asserted on the
    dry-run path below, and here we assert the engine's *actual* commit-path
    guarantee: zero verdict rows.
    """
    # Only `set` ops execute a tool here; `del` would too, but to keep the
    # executed count exactly len(prog) we drive every op through the one tool.
    conn = fresh_kv_conn()
    audit = AuditJournal.in_memory()
    try:
        with agent_txn({"sql": SQLiteAdapter(conn)}, audit=audit) as txn:
            for op in prog:
                kv_tool_only(k=op.key, v=op.value)
            tid = txn.txn_id
        rows = audit.get_effects(tid)
        # Effect-completeness: one journal row per executed tool call.
        assert len(rows) == len(prog)
        assert [r["idx"] for r in rows] == list(range(len(prog)))
        assert all(r["status"] == "APPLIED" for r in rows)
        # Reported gap: the commit path captures no verdicts.
        assert audit.get_verdicts(tid) == []
    finally:
        conn.close()


@pytest.fixture
def kv_tool_and_policy():
    """One reversible kv tool plus an all-Allow policy carrying R explicit rules.

    The rules are pure Allow predicates so the program always commits-clean on
    the dry-run path; their only job is to force a deterministic, countable
    verdict population (one verdict per effect per rule per phase).
    """

    @tool(resource="sql")
    def kv_put(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        return v

    policy = Policy.allow_all()

    @policy.rule
    def rule_a(effect, ctx):
        return Allow()

    @policy.rule
    def rule_b(effect, ctx):
        return Allow()

    return kv_put, policy, 2  # R = 2 explicit rules


@pytest.mark.audit
@given(prog=kv_programs(min_size=0, max_size=8))
@_LAW_AUDIT
def test_audit_completeness_dryrun_verdicts(kv_tool_and_policy, prog):
    """THEOREM (audit completeness, dry-run path): every effect AND every verdict recorded.

    The dry-run path captures verdicts. For N executed effects and R explicit
    all-Allow rules, the engine evaluates each effect against each rule at both
    stage-time and commit-time, so the journal must hold exactly:
        effects  == N
        verdicts == 2 · N · R   (phase ∈ {stage, commit})
    and each verdict carries a valid effect_index and phase. This pins
    verdict-completeness as a hard equality, not a "≥".
    """
    from pherix.core.dry_run import dry_run

    kv_put, policy, R = kv_tool_and_policy
    conn = fresh_kv_conn()
    audit = AuditJournal.in_memory()
    try:
        with dry_run({"sql": SQLiteAdapter(conn)}, policy=policy, audit=audit) as ctx:
            for op in prog:
                kv_put(k=op.key, v=op.value)
            tid = ctx.txn_id
        N = len(prog)
        effects = audit.get_effects(tid)
        verdicts = audit.get_verdicts(tid)
        # Effect-completeness on the dry-run path too.
        assert len(effects) == N
        # Verdict-completeness: exactly one verdict per (effect, rule, phase).
        assert len(verdicts) == 2 * N * R
        phases = {v["phase"] for v in verdicts}
        if N > 0:
            assert phases == {"stage", "commit"}
            # Every verdict points at a real effect index, and every effect
            # is covered at both phases by every rule.
            indices = {e["idx"] for e in effects}
            for v in verdicts:
                assert v["effect_index"] in indices
            for phase in ("stage", "commit"):
                per_phase = [v for v in verdicts if v["phase"] == phase]
                assert len(per_phase) == N * R
    finally:
        conn.close()


# ===========================================================================
# THEOREM 3 — OVERSIGHT  (the wedge — gap-fill: adversarial gate fuzz)
#   Over arbitrary mixed sequences, an irreversible effect fires at commit IFF
#   it had a registered compensator OR it was explicitly approved. Otherwise
#   commit raises GateBlocked, nothing un-approved fired, and the world is
#   untouched. Necessary AND sufficient, under any interleaving.
# ===========================================================================


@pytest.fixture
def gate_tools():
    """The fixed mixed toolset for the oversight law, registered once.

    A call-log records every irreversible fire so the body can assert exactly
    which effects actually fired. The reversible lane writes to the SQL world
    passed at call-time; the irreversible lanes append to the shared log.
    """
    fired: list[tuple[str, str, int]] = []

    @tool(resource="sql")
    def rev_set(conn, k, v):
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        return v

    @tool(resource="http", reversible=False, injects_handle=False)
    def refund(account, amount):
        fired.append(("refund", account, amount))

    @tool(
        resource="http",
        reversible=False,
        injects_handle=False,
        compensator="refund",
    )
    def charge_comp(account, amount):
        fired.append(("charge_comp", account, amount))
        return {"ok": True}

    @tool(resource="http", reversible=False, injects_handle=False)
    def charge_nocomp(account, amount):
        fired.append(("charge_nocomp", account, amount))
        return {"ok": True}

    return rev_set, charge_comp, charge_nocomp, fired


@pytest.mark.oversight
@given(prog=mixed_programs(min_size=0, max_size=8))
@_LAW
def test_oversight_gate_fuzz(gate_tools, prog):
    """THEOREM (oversight): no irreversible effect commits without a compensator
    or explicit approval — under ANY interleaving.

    For every generated mixed program we predict, from the program alone, the
    one fact that decides the outcome: is there any irreversible no-compensator
    action that the operator does NOT approve? If so the gate must block; else
    every irreversible action must fire exactly once and the txn must commit.

    Necessity: an un-approved no-compensator effect ⇒ GateBlocked, nothing
    un-approved fired, the reversible world is rolled back to baseline.
    Sufficiency: a compensator OR an approval ⇒ the effect fires exactly once
    and the commit lands.
    """
    rev_set, charge_comp, charge_nocomp, fired = gate_tools
    fired.clear()

    conn = fresh_kv_conn()
    try:
        baseline = dump_kv(conn)  # empty, but stated explicitly

        # An un-approved IRR_NOCOMP anywhere must gate the whole commit.
        any_unapproved = any(
            a.kind is ActionKind.IRR_NOCOMP and not a.approve for a in prog
        )

        # The irreversible effects that *should* fire if the commit proceeds,
        # in journal (index) order — every IRR_COMP and every IRR_NOCOMP.
        expected_fires: list[tuple[str, str, int]] = []
        for a in prog:
            if a.kind is ActionKind.IRR_COMP:
                expected_fires.append(("charge_comp", a.key, a.value))
            elif a.kind is ActionKind.IRR_NOCOMP:
                expected_fires.append(("charge_nocomp", a.key, a.value))

        def _drive(txn):
            staged: list[tuple[StagedResult, bool]] = []
            for a in prog:
                if a.kind is ActionKind.REV:
                    rev_set(k=a.key, v=a.value)
                elif a.kind is ActionKind.IRR_COMP:
                    charge_comp(account=a.key, amount=a.value)
                else:  # IRR_NOCOMP
                    res = charge_nocomp(account=a.key, amount=a.value)
                    staged.append((res, a.approve))
            # Approve exactly the IRR_NOCOMP effects the program said to.
            for res, approve in staged:
                if approve:
                    txn.approve_irreversible(res.effect_id)

        if any_unapproved:
            # NECESSITY: the gate blocks the entire commit.
            with pytest.raises(GateBlocked):
                with agent_txn({"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}) as txn:
                    _drive(txn)
            # Nothing un-approved fired — in fact, on a gate-block NO staged
            # irreversible fires at all (the gate is checked before the fire
            # fold), so the call-log is empty.
            assert fired == []
            # The reversible world is rolled back to its committed baseline.
            assert dump_kv(conn) == baseline
            assert txn.txn.state is TxnState.ROLLED_BACK
            # No effect is left APPLIED; staged irreversibles are GATED or
            # STAGED, reversibles COMPENSATED — nothing torn.
            assert all(
                e.status is not EffectStatus.APPLIED for e in txn.txn.effects
            )
        else:
            # SUFFICIENCY: every irreversible has a compensator or an approval,
            # so the commit lands and each irreversible fires exactly once,
            # in journal order.
            with agent_txn({"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}) as txn:
                _drive(txn)
            assert txn.txn.state is TxnState.COMMITTED
            assert fired == expected_fires
            # Each irreversible journal effect ends APPLIED (it fired); the
            # reversibles are APPLIED too.
            assert all(
                e.status is EffectStatus.APPLIED for e in txn.txn.effects
            )
    finally:
        conn.close()
