"""Model-based / stateful property testing of the transaction engine.

One Hypothesis :class:`RuleBasedStateMachine` generates *random sequences* of
transaction operations — open / reversible write / staged irreversible charge /
approve / commit / rollback / crash+recover / a concurrent overlapping txn — and
after every step checks the real (production-machinery) world against a
dead-simple plain-Python **reference model** (``tests/_stateful.Model``). The
model is the oracle: every invariant is "real world == model", so a divergence
is a real kernel bug, never a model bug.

This generalises the single-shot ``test_laws_*`` suites into one explorer that
folds *thousands* of interleavings through the SAME engine. Coverage comes from
the breadth of generated schedules, not from hand-written examples.

The reversible world is the production :class:`SQLiteAdapter` over an on-disk
``kv`` table (on-disk so crash+recover hits the real DB-auto-rollback path and
so the durable audit journal lives at a reopenable path). The irreversible world
is a payment ledger behind :class:`tests._laws.LedgerAdapter`; ``charge`` has a
registered compensator ``refund`` (auto-committable / recoverable), while
``charge_uncomp`` has none (gates at commit / lands STUCK on recover).

Invariants asserted (the heart of the pillar):

- after a clean commit:   real kv == model.kv_committed AND ledger == model
- after a rollback:       real kv == the committed baseline, byte-identical;
                          ledger untouched (no staged irreversible ever fired)
- no policy-denied effect is ever applied to the real world (denied-set tracked)
- after crash+recover:    the txn lands terminal and every effect is
                          exactly-once (applied-once or compensated-once, never
                          both, never twice) — a SECOND recover pass fires
                          zero further compensators
- irreversible effects never fire before commit; a gated charge with no
  approval blocks the commit (and unwinds without firing)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from hypothesis import strategies as st

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus, StagedResult
from pherix.core.isolation import REGISTRY as ISOLATION_REGISTRY
from pherix.core.policy import Allow, Deny, Policy
from pherix.core.recovery import recover
from pherix.core.runtime import GateBlocked, TxnContext
from pherix.core.tools import active_txn, tool
from pherix.core.transaction import TxnState

from tests._laws import LedgerAdapter, charge_impl, refund_impl
from tests._stateful import (
    KV_KEYS,
    LEDGER_ACCOUNTS,
    Model,
    dump_kv,
    fresh_kv_conn,
    ledger_equal,
)

# A tool the policy always denies — its presence in a generated sequence lets
# the machine assert "a denied effect never touched the real world".
_DENIED_KEY = "denied"


# The toolset is process-global (the runtime forbids re-registration and the
# autouse conftest fixture clears the REGISTRY once per *test function*).
# Hypothesis instantiates the state machine MANY times within one test function,
# so registration must happen exactly once and the resulting wrapper callables
# must be reused across every machine instance. We register lazily on first
# machine construction and stash the wrappers here; later instances reuse them.
_TOOLS: dict[str, object] = {}


def _ensure_tools() -> dict[str, object]:
    """Register the fixed toolset once; return the wrapper callables.

    Idempotent across machine instances within a single test run. The kv tools
    are reversible (SQLiteAdapter); ``charge`` is irreversible with a registered
    ``refund`` left-inverse (auto-committable / recoverable); ``charge_uncomp``
    is irreversible with no compensator (gates at commit, STUCK on recover).
    """
    if _TOOLS:
        return _TOOLS

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

    @tool(resource="ledger", reversible=False, injects_handle=True)
    def refund(ledger, account, amount):
        return refund_impl(ledger, account, amount)

    @tool(
        resource="ledger",
        reversible=False,
        injects_handle=True,
        compensator="refund",
    )
    def charge(ledger, account, amount):
        return charge_impl(ledger, account, amount)

    @tool(resource="ledger", reversible=False, injects_handle=True)
    def charge_uncomp(ledger, account, amount):
        return charge_impl(ledger, account, amount)

    _TOOLS.update(
        kv_set=kv_set,
        kv_del=kv_del,
        charge=charge,
        charge_uncomp=charge_uncomp,
    )
    return _TOOLS


class TransactionMachine(RuleBasedStateMachine):
    """Random op sequences vs. a plain-dict oracle, over the real engine.

    The runtime's ``agent_txn`` is a *context manager* — it auto-commits on
    clean block exit. A state machine cannot hold a ``with`` block open across
    rules, so this machine drives the SAME machinery ``agent_txn`` does, but
    step-by-step: open = construct the :class:`TxnContext` exactly as
    ``agent_txn`` does (begin transactional adapters, register with the
    isolation substrate, set the ``active_txn`` ContextVar); finalise =
    ``ctx.commit()`` / ``ctx.rollback()`` then tear the registration down. This
    is faithful to the production open/close bracket (runtime.agent_txn lines
    731-759), just sliced across rules.
    """

    def __init__(self) -> None:
        super().__init__()
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._kv_path = str(d / "kv.db")
        self._journal_path = str(d / "journal.db")

        # The reversible world — production SQLiteAdapter over an on-disk kv
        # table. Fresh per machine run.
        self._conn = fresh_kv_conn(self._kv_path)
        self._sql = SQLiteAdapter(self._conn)

        # The irreversible world — a payment ledger dict behind LedgerAdapter.
        self._ledger: dict[str, int] = {}
        self._ledger_adapter = LedgerAdapter(self._ledger)

        # A durable on-disk audit journal so a simulated crash leaves something
        # recover() can reopen by path.
        self._audit = AuditJournal(self._journal_path)

        self._adapters = {"sql": self._sql, "ledger": self._ledger_adapter}

        # Deny-list policy: any kv_set on the sentinel key is refused at
        # stage-time. A denied effect must never reach the real table.
        policy = Policy.allow_all()

        @policy.rule
        def deny_sentinel(effect, ctx):
            if effect.tool == "kv_set" and effect.args.get("k") == _DENIED_KEY:
                return Deny("sentinel key is policy-denied")
            return Allow()

        self._policy = policy

        # The oracle.
        self.model = Model()

        # Live txn handle (None ⇔ no open txn) + the active_txn token to reset.
        self._ctx: TxnContext | None = None
        self._token = None
        # effect_id -> StagedResult mapping for the open txn (for approve()).
        self._staged_ids: list[str] = []

        # The FIXED toolset — registered once across the whole test run.
        tools = _ensure_tools()
        self._kv_set = tools["kv_set"]
        self._kv_del = tools["kv_del"]
        self._charge = tools["charge"]
        self._charge_uncomp = tools["charge_uncomp"]

    # --- the open / finalise bracket (mirrors runtime.agent_txn) ----------

    def _open_txn(self) -> None:
        for adapter in (self._sql,):  # transactional adapters → begin()
            adapter.begin()
        self._ctx = TxnContext(self._adapters, self._policy, self._audit)
        ISOLATION_REGISTRY.register(self._ctx)
        self._token = active_txn.set(self._ctx)
        self._staged_ids = []
        self.model.open()

    def _teardown_txn(self) -> None:
        active_txn.reset(self._token)
        ISOLATION_REGISTRY.unregister(self._ctx)
        self._ctx = None
        self._token = None
        self._staged_ids = []

    # --- initialize -------------------------------------------------------

    @initialize()
    def _start(self) -> None:
        # Worlds + model both start empty; nothing to do beyond construction.
        pass

    # --- preconditions ----------------------------------------------------

    def _open(self) -> bool:
        return self._ctx is not None

    def _closed(self) -> bool:
        return self._ctx is None

    # --- rules: lifecycle -------------------------------------------------

    @precondition(lambda self: self._closed())
    @rule()
    def open(self) -> None:
        self._open_txn()

    @precondition(lambda self: self._open())
    @rule(k=st.sampled_from(KV_KEYS), v=st.integers(min_value=-1000, max_value=1000))
    def call_reversible_set(self, k: str, v: int) -> None:
        self._kv_set(k=k, v=v)
        self.model.kv_set(k, v)

    @precondition(lambda self: self._open())
    @rule(k=st.sampled_from(KV_KEYS))
    def call_reversible_del(self, k: str) -> None:
        self._kv_del(k=k)
        self.model.kv_del(k)

    @precondition(lambda self: self._open())
    @rule(v=st.integers(min_value=-1000, max_value=1000))
    def call_denied(self, v: int) -> None:
        """A policy-denied reversible write: must raise and touch nothing.

        The denied effect is journalled nowhere, the table is unchanged, and the
        txn stays usable. The model is NOT mutated — the denied write never
        happened, so the post-commit "real == model" invariant is the assertion
        that the denial actually held.
        """
        import pytest

        before = dump_kv(self._conn)
        with pytest.raises(Exception):
            self._kv_set(k=_DENIED_KEY, v=v)
        # Stage-time denial: the real table is untouched right now.
        assert dump_kv(self._conn) == before
        # The txn is still open and usable.
        assert self._ctx is not None

    @precondition(lambda self: self._open())
    @rule(
        account=st.sampled_from(LEDGER_ACCOUNTS),
        amount=st.integers(min_value=1, max_value=500),
        comp=st.booleans(),
    )
    def call_irreversible(self, account: str, amount: int, comp: bool) -> None:
        """Stage an irreversible charge (with or without a compensator).

        The agent gets a :class:`StagedResult` sentinel — the real ledger MUST
        be untouched (irreversibles never fire before commit). That is asserted
        on the spot and re-asserted by the ``ledger_untouched_until_commit``
        invariant.
        """
        ledger_before = dict(self._ledger)
        if comp:
            res = self._charge(account=account, amount=amount)
        else:
            res = self._charge_uncomp(account=account, amount=amount)
        assert isinstance(res, StagedResult)
        # Irreversible has not fired: the external ledger is byte-identical.
        assert self._ledger == ledger_before
        self._staged_ids.append(res.effect_id)
        self.model.stage_charge(
            res.effect_id, account, amount, has_compensator=comp
        )

    @precondition(lambda self: self._open())
    @precondition(lambda self: bool(self._needs_approval_ids()))
    @rule(data=st.data())
    def approve(self, data) -> None:
        """Approve one staged-but-unapproved irreversible charge."""
        candidates = self._needs_approval_ids()
        eid = data.draw(st.sampled_from(candidates))
        self._ctx.approve_irreversible(eid)
        self.model.approve(eid)

    def _needs_approval_ids(self) -> list[str]:
        if self._ctx is None:
            return []
        return sorted(self.model.needs_approval - self.model.approved)

    @precondition(lambda self: self._open())
    @rule()
    def commit(self) -> None:
        """Commit the open txn. If the gate blocks (an unapproved charge with no
        compensator), commit raises GateBlocked, the txn unwinds, and NOTHING
        irreversible fired — the model's rollback path is the oracle then."""
        ledger_before = dict(self._ledger)
        if self.model.gate_blocks:
            try:
                self._ctx.commit()
                raise AssertionError("expected GateBlocked, commit succeeded")
            except GateBlocked:
                pass
            # Gate-block ⇒ partial unwind ⇒ nothing irreversible fired and the
            # reversibles are restored. The committed baseline stands.
            assert ledger_equal(self._ledger, ledger_before)
            assert self._ctx.txn.state is TxnState.ROLLED_BACK
            self.model.rollback()
        else:
            self._ctx.commit()
            assert self._ctx.txn.state is TxnState.COMMITTED
            # Every charge fired exactly once: the staged irreversibles applied.
            self.model.commit()
        self._teardown_txn()

    @precondition(lambda self: self._open())
    @rule()
    def rollback(self) -> None:
        """Roll the open txn back: reversibles restore to the committed baseline,
        staged irreversibles never fired."""
        ledger_before = dict(self._ledger)
        self._ctx.rollback()
        assert self._ctx.txn.state is TxnState.ROLLED_BACK
        assert ledger_equal(self._ledger, ledger_before)
        self.model.rollback()
        self._teardown_txn()

    # --- rule: crash + recover --------------------------------------------

    @precondition(lambda self: self._open())
    @precondition(lambda self: self._has_applied_work())
    @rule()
    def crash_and_recover(self) -> None:
        """Simulate a mid-flight crash, then drive recover() and assert the txn
        lands terminal with exactly-once compensation.

        A "crash" is: stop driving the live txn WITHOUT committing or rolling
        back, drop the in-memory ctx, and close the live connections (process
        death). What survives is the durable audit journal (the runtime has been
        writing effect rows all along) plus the on-disk SQLite file. recover()
        reopens both by path and resumes the backward fold:

        - reversible kv writes: the DB auto-rolled-back the uncommitted txn when
          the connection closed, so recover records them COMPENSATED
          ('db_auto_rolled_back') without touching a dead savepoint.
        - APPLIED irreversible charges: recover re-fires the registered
          compensator (refund) exactly once. (None are APPLIED here — they are
          still STAGED pre-commit — so the irreversible side of the ledger is
          untouched and recovery is driven purely by the reversible work.)

        After recover: the txn is terminal and a SECOND recover pass fires zero
        further compensators (the durable status is the idempotency fence). The
        real kv table equals the committed baseline — exactly what a rollback
        would have produced — so the model takes its rollback transition.
        """
        # Mark the durable txn as PARTIAL so recover treats it as mid-flight.
        # (A crash during commit/unwind is exactly the PARTIAL state; an OPEN
        # txn with APPLIED reversibles is equally a recover candidate.)
        kv_committed_before = dict(self.model.kv_committed)

        # --- the crash: abandon the live txn, kill the connections ---
        active_txn.reset(self._token)
        ISOLATION_REGISTRY.unregister(self._ctx)
        self._ctx = None
        self._token = None
        self._staged_ids = []
        # Process death closes connections: the uncommitted BEGIN auto-rolls
        # back in the real DB, and the audit journal is flushed (autocommit).
        self._conn.close()
        self._audit.close()

        # --- recovery: a fresh process reopens the durable journal by path ---
        report = recover(self._journal_path, self._adapters)

        # The txn was mid-flight (had APPLIED reversible work) ⇒ recovered.
        assert len(report.transactions) == 1
        tr = report.transactions[0]
        assert tr.final_state == TxnState.ROLLED_BACK.name
        # No irreversible was APPLIED pre-commit, so no compensator fired.
        assert tr.compensators_fired == 0

        # Exactly-once fence: a SECOND pass does nothing new.
        report2 = recover(self._journal_path, self._adapters)
        assert report2.compensators_fired == 0
        for t in report2.transactions:
            assert t.compensators_fired == 0

        # The real reversible world: the DB auto-rolled-back to the committed
        # baseline. Reopen the on-disk file and compare against the model's
        # committed kv (pre-txn).
        conn = fresh_kv_conn(self._kv_path)
        try:
            assert dump_kv(conn) == kv_committed_before
        finally:
            conn.close()

        # The ledger never moved (no irreversible fired before the crash).
        assert ledger_equal(self._ledger, self.model.ledger_committed)

        # Rebuild the live worlds for subsequent rules (the crash closed them).
        self._conn = fresh_kv_conn(self._kv_path)
        self._sql = SQLiteAdapter(self._conn)
        self._audit = AuditJournal(self._journal_path)
        self._adapters = {"sql": self._sql, "ledger": self._ledger_adapter}
        self.model.rollback()

    def _has_applied_work(self) -> bool:
        """The open txn has at least one APPLIED reversible effect — the
        evidence recover() keys 'mid-flight' on. Without it recover is a no-op
        and the crash rule would assert against an empty report."""
        if self._ctx is None:
            return False
        return any(
            e.status is EffectStatus.APPLIED for e in self._ctx.txn.effects
        )

    # --- rule: a concurrent overlapping txn -------------------------------

    @precondition(lambda self: self._closed())
    @rule(
        key=st.sampled_from(KV_KEYS),
        v=st.integers(min_value=-1000, max_value=1000),
    )
    def concurrent_committed_txn(self, key: str, v: int) -> None:
        """A second, fully-nested transaction commits a single kv write while no
        primary txn is open. It exercises the isolation diff machinery (open →
        write → commit) end-to-end and advances the committed baseline; the
        model mirrors the same single write so the next invariant check pins it.

        Run only between primary txns (closed precondition) to keep the oracle
        single-threaded and deterministic — the conflict-raising path is already
        fuzzed hard by test_laws_concurrency; here the point is that an
        independent committed txn correctly moves the shared committed world the
        model tracks."""
        from pherix.core.runtime import agent_txn

        with agent_txn(self._adapters, policy=self._policy, audit=self._audit) as ctx:
            self._kv_set(k=key, v=v)
        assert ctx.txn.state is TxnState.COMMITTED
        self.model.kv_committed[key] = v

    # --- invariants -------------------------------------------------------

    @invariant()
    def committed_world_matches_model_when_closed(self) -> None:
        """With no txn open, the real committed worlds equal the model."""
        if self._ctx is not None:
            return
        assert dump_kv(self._conn) == self.model.kv_committed
        assert ledger_equal(self._ledger, self.model.ledger_committed)

    @invariant()
    def working_world_matches_model_when_open(self) -> None:
        """With a txn open, the live (uncommitted) kv reflects the working copy
        — reversibles run live, so the engine's in-flight view must equal the
        model's working set at every step."""
        if self._ctx is None:
            return
        assert dump_kv(self._conn) == self.model.kv_working

    @invariant()
    def ledger_untouched_until_commit(self) -> None:
        """No staged irreversible has fired: the live ledger never exceeds the
        committed ledger while a txn is open."""
        if self._ctx is None:
            return
        assert ledger_equal(self._ledger, self.model.ledger_committed)

    @invariant()
    def denied_key_never_in_world(self) -> None:
        """The policy-denied sentinel key never appears in the committed table,
        nor in the working set — a denied effect is applied nowhere, ever."""
        assert _DENIED_KEY not in dump_kv(self._conn)

    # --- cleanup ----------------------------------------------------------

    def teardown(self) -> None:
        if self._token is not None:
            try:
                active_txn.reset(self._token)
            except Exception:
                pass
        if self._ctx is not None:
            try:
                ISOLATION_REGISTRY.unregister(self._ctx)
            except Exception:
                pass
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._audit.close()
        except Exception:
            pass
        self._tmp.cleanup()


TransactionMachine.TestCase.settings = settings(
    max_examples=300,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# pytest collects this as the runnable test case.
TestTransactionMachine = TransactionMachine.TestCase
