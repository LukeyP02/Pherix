"""Mechanism test (deterministic, offline, no SDK) for the concurrent scenario.

NOT a real-agent run. The harm here — a *lost update* — is a property of two
writes to one ledger key without isolation, so it reproduces deterministically
with no model in the loop: this scenario reuses the audit module's tested
``run_contended_reconciliation`` through the ``run_arm_override`` seam. We assert
the framework's two-arm contract end-to-end by driving ``run_arm(SCENARIO, ...)``
(so the override delegation itself is exercised):

  * ungoverned — both reconcilers' -50 corrections land, the contended entry
    over-corrects, and the *independent* oracle flags every run;
  * governed — ``Abort`` MVCC isolation aborts the stale committer, exactly one
    correction lands, harm is 0, and each run records the conflict as a
    boundary-push with verdict "contained";
  * the oracle is independent of the policy — two corrections posted straight to
    a real ledger (no transaction, no isolation) make it flag; one is clean.

No ``anthropic`` import, no key, no network.
"""

from examples.dogfood.audit import CONTENDED_ENTRY, EXPECTED_AMOUNTS, LEDGER_SCHEMA
from examples.dogfood.infra import scratch_sqlite
from examples.dogfood.sims.concurrent import SCENARIO
from examples.dogfood.sims.scenario import Scenario, all_scenarios, run_arm


# --- discovery -------------------------------------------------------------


def test_concurrent_is_discovered():
    found = all_scenarios()
    assert "concurrent" in found
    assert isinstance(found["concurrent"], Scenario)
    # It owns its own execution shape via the override seam — the load-bearing
    # difference from every single-agent scenario.
    assert found["concurrent"].run_arm_override is not None
    assert found["concurrent"].provider == "anthropic"


# --- ungoverned: the lost update lands and the independent oracle flags it ---


def test_ungoverned_lost_update_lands_and_oracle_flags_it():
    arm = run_arm(SCENARIO, governed=False, runs=2)
    # Both reconcilers' -50 landed in every run; the contended entry over-corrected
    # below its expected value — the independent oracle flags all of them.
    assert arm.harmed == 2
    assert arm.harm_rate == 1.0
    proof = arm.outcomes[0].proof
    assert proof["entry"] == CONTENDED_ENTRY
    assert proof["expected"] == EXPECTED_AMOUNTS[CONTENDED_ENTRY]
    # Over-correction: two -50s booked, effective driven below expected.
    assert proof["effective"] < proof["expected"]
    assert len(proof["adjustments"]) == 2


# --- governed: Abort isolation contains it, harm == 0, conflict pushed -------


def test_governed_isolation_prevents_lost_update():
    arm = run_arm(SCENARIO, governed=True, runs=2)
    # The stale committer was aborted each run, so exactly one -50 landed and the
    # SAME oracle sees a clean, correctly-corrected entry — no harm.
    assert arm.harmed == 0
    assert arm.harm_rate == 0.0
    # One isolation conflict per run is the boundary-push: the agent genuinely
    # attempted the redundant write and was contained.
    assert arm.boundary_pushes >= 2
    assert all(o.verdict == "contained" for o in arm.outcomes)
    proof = arm.outcomes[0].proof
    assert proof["effective"] == proof["expected"]
    assert proof["conflict"] is True
    assert len(proof["adjustments"]) == 1


# --- the oracle is independent of the policy (rule 3) -----------------------


def test_oracle_is_independent_of_policy():
    """Drive harm straight into a real ledger — no transaction, no isolation.

    A single correction reaches the contended entry's expected value and is clean;
    a second identical correction over-corrects it — and the oracle flags that
    with no policy or isolation engine involved at all. Uses a real on-disk
    ledger (the oracle re-reads through a fresh connection per call), parameterised
    inserts straight to the ``adjustments`` log — no transaction, no isolation.
    """
    with scratch_sqlite(LEDGER_SCHEMA) as db:
        # One correct -50 against the seeded-550 contended entry → expected 500.
        db.conn.execute(
            "INSERT INTO adjustments (entry_id, delta, reason, client_id) "
            "VALUES (?, ?, ?, ?)",
            (CONTENDED_ENTRY, -50, "single correction", "a"),
        )
        harmed, proof = SCENARIO.harm_oracle(db)
        assert harmed is False
        assert proof["effective"] == EXPECTED_AMOUNTS[CONTENDED_ENTRY]

        # A second identical -50 over-corrects — the lost-update signature.
        db.conn.execute(
            "INSERT INTO adjustments (entry_id, delta, reason, client_id) "
            "VALUES (?, ?, ?, ?)",
            (CONTENDED_ENTRY, -50, "redundant correction", "b"),
        )
        harmed, proof = SCENARIO.harm_oracle(db)
        assert harmed is True
        assert proof["effective"] < proof["expected"]
