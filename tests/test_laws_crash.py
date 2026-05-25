"""Crash / chaos fuzzing of the durable backward fold (``recover``).

A crash can strike at *any* point of the commit/unwind fold. The durable
journal is the only thing that survives, and ``recover`` resumes the fold from
it. The laws under test, fuzzed over arbitrary durable journals:

- **terminal landing** — every recoverable transaction ends in a terminal
  state (``ROLLED_BACK`` if every standing effect was undone, ``STUCK`` if an
  irreversible effect has no compensator).
- **exactly-once compensation** — each APPLIED irreversible effect is
  compensated exactly once. Running ``recover`` a second time fires *zero*
  further compensators: the durable ``status`` is the idempotency fence.
- **the fence is honoured** — an effect already durably ``COMPENSATED`` is
  never re-compensated; a ``STAGED`` / ``FAILED`` effect put nothing in the
  world and is never compensated.

We hand-build durable journals (the only state a dead process leaves) and run
``recover`` against a fresh adapter + the process registry — the same "new
process after a crash" model the unit recovery tests use.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pherix.core.audit import AuditJournal
from pherix.core.effects import Effect, EffectStatus
from pherix.core.recovery import recover
from pherix.core.tools import tool
from pherix.core.transaction import Transaction, TxnState

# Trust pillar: audit — durability: recovery folds the durable journal to a
# consistent world after a crash at any step.
pytestmark = pytest.mark.audit

_LAW = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class CountingAdapter:
    """Irreversible adapter that records every compensator fire — exactly-once
    is observable as the length of ``applied``."""

    name = "ext"

    def __init__(self) -> None:
        self.applied: list[tuple[str, dict]] = []

    def supports_rollback(self) -> bool:
        return False

    def apply(self, effect: Effect, tool_fn):
        self.applied.append((effect.tool, dict(effect.args)))
        return tool_fn(**effect.args)


@pytest.fixture
def crash_tools():
    """Toolset spanning every recovery branch — registered once per function."""

    @tool(resource="ext", reversible=False, injects_handle=False)
    def refund(amount):
        return None

    @tool(
        resource="ext",
        reversible=False,
        injects_handle=False,
        compensator="refund",
    )
    def charge(amount):
        return None

    # An irreversible effect with NO compensator — its standing side effect
    # forces a STUCK landing on recovery.
    @tool(resource="ext", reversible=False, injects_handle=False)
    def charge_no_comp(amount):
        return None

    # A reversible effect — the DB auto-rolled it back on process death, so
    # recovery records it COMPENSATED without touching any adapter.
    @tool(resource="sql")
    def kv_write(conn, amount):
        return None


# Each generated effect: (tool, resource, reversible), a durable status, amount.
_TOOL_KINDS = st.sampled_from(
    [
        ("charge", "ext", False),
        ("charge_no_comp", "ext", False),
        ("kv_write", "sql", True),
    ]
)
_STATUSES = st.sampled_from(
    [
        EffectStatus.APPLIED,
        EffectStatus.COMPENSATED,
        EffectStatus.STAGED,
        EffectStatus.FAILED,
    ]
)


def _journal_specs():
    return st.lists(
        st.tuples(_TOOL_KINDS, _STATUSES, st.integers(min_value=1, max_value=10_000)),
        min_size=1,
        max_size=8,
    )


def _build_journal(db_path: str, specs) -> str:
    """Persist a durable journal that looks like a crash left it: a PARTIAL
    transaction with the given per-effect statuses. Returns the txn_id."""
    audit = AuditJournal(db_path)
    txn = Transaction()
    txn.state = TxnState.PARTIAL
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, TxnState.PARTIAL.name)
    for idx, ((tname, resource, reversible), status, amount) in enumerate(specs):
        eff = Effect(
            txn_id=txn.txn_id,
            index=idx,
            tool=tname,
            args={"amount": amount},
            resource=resource,
            reversible=reversible,
            status=status,
        )
        audit.record_effect(eff)
        audit.update_effect(eff)
    audit.close()
    return txn.txn_id


@given(specs=_journal_specs())
@_LAW
def test_recover_lands_terminal_with_exactly_once_compensation(crash_tools, specs):
    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "journal.db")
        txn_id = _build_journal(db_path, specs)

        # What recovery *should* do, derived from the durable statuses.
        applied = [s for s in specs if s[1] is EffectStatus.APPLIED]
        comp_targets = [
            s for s in applied if s[0][0] == "charge"  # has a compensator
        ]
        stuck = any(s[0][0] == "charge_no_comp" for s in applied)
        has_applied = bool(applied)

        adapter = CountingAdapter()
        report = recover(db_path, {"ext": adapter})

        if not has_applied:
            # No standing work ⇒ nothing is mid-flight ⇒ recovery is a no-op.
            assert report.transactions == []
            return

        assert len(report.transactions) == 1
        tr = report.transactions[0]
        assert tr.txn_id == txn_id
        # Exactly one refund per APPLIED 'charge'; none for reversible or
        # already-compensated or staged effects.
        assert len(adapter.applied) == len(comp_targets)
        assert all(name == "refund" for name, _ in adapter.applied)
        assert tr.compensators_fired == len(comp_targets)
        expected_final = TxnState.STUCK.name if stuck else TxnState.ROLLED_BACK.name
        assert tr.final_state == expected_final

        # Exactly-once fence: a SECOND recovery pass fires no further
        # compensators — the durable COMPENSATED status skips them.
        adapter2 = CountingAdapter()
        recover(db_path, {"ext": adapter2})
        assert adapter2.applied == []


@given(n=st.integers(min_value=1, max_value=6), crash_at=st.integers(min_value=0, max_value=6))
@_LAW
def test_crash_at_every_unwind_index_is_exactly_once(crash_tools, n, crash_at):
    """Walk the crash point across the whole backward fold.

    Model: ``n`` irreversible charges all fired (APPLIED). The unwind runs
    newest-first; a crash after undoing the newest ``k`` effects leaves a
    suffix of ``k`` COMPENSATED and a prefix of ``n-k`` still APPLIED.
    Recovery must compensate exactly the ``n-k`` standing effects — never the
    ``k`` already undone — and land ROLLED_BACK, for every crash point ``k``.
    """
    k = min(crash_at, n)  # number already undone before the crash
    specs = []
    for idx in range(n):
        # Newest k effects (highest indices) were already compensated.
        status = (
            EffectStatus.COMPENSATED if idx >= n - k else EffectStatus.APPLIED
        )
        specs.append((("charge", "ext", False), status, 100 + idx))

    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "journal.db")
        _build_journal(db_path, specs)

        adapter = CountingAdapter()
        report = recover(db_path, {"ext": adapter})

        still_standing = n - k
        if still_standing == 0:
            # Nothing APPLIED ⇒ not mid-flight ⇒ no-op recovery.
            assert report.transactions == []
            return
        assert len(adapter.applied) == still_standing
        assert report.transactions[0].final_state == TxnState.ROLLED_BACK.name

        adapter2 = CountingAdapter()
        recover(db_path, {"ext": adapter2})
        assert adapter2.applied == []
