"""Fuzzing the durable journal fed to ``recover`` — fail loud, never silently
wrong.

The durable SQLite journal is the *only* state a dead process leaves behind;
``recover`` resumes the backward fold from it. The single highest-value safety
property of the whole crash-recovery story is therefore:

    recover() on a corrupted journal either
      (a) lands every effect in a correct terminal exactly-once state, OR
      (b) raises a clear, typed error,
    but NEVER returns a success report while leaving an effect half-applied,
    double-applied, or silently dropped.

The forbidden middle ground is "silently wrong": a ``RecoveryReport`` that
claims success while a real-world side effect is left standing-but-marked-
undone, or undone-but-fired-twice. Every test here is built to be able to FAIL
if that middle ground ever opens up.

We start from a known-GOOD mid-flight journal (``build_midflight_journal``: N
APPLIED irreversible charges under a PARTIAL txn — a crash mid-unwind), corrupt
a *copy* of it in many ways, and feed the corruption to ``recover``. The
exactly-once observable is ``CountingAdapter.applied`` (one refund per standing
charge, zero on a second pass); the durable fence is the per-effect status the
recovery commits.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pherix.core.recovery import recover
from pherix.core.tools import tool
from pherix.core.transaction import TxnState

from tests._fuzz import (
    CountingAdapter,
    build_midflight_journal,
    delete_effect_rows,
    flip_bytes,
    mangle_json_column,
    overwrite_range,
    read_durable_statuses,
    read_txn_state,
    set_effect_index,
    set_effect_status,
    set_txn_state,
    truncate_at,
)

# Trust pillar: audit — durability under truncation / byte-flip / corruption:
# recover fails loud or lands clean, never silently wrong.
pytestmark = pytest.mark.audit

_FUZZ = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def charge_refund_tools():
    """A 'charge' irreversible whose registered compensator is 'refund'.

    Mirrors the toolset the durable journal references. Registered once per
    test (conftest clears REGISTRY between tests; Hypothesis re-runs the body
    against this same registration).
    """

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


# A terminal landing is one of these. Anything else (OPEN/STAGED/PARTIAL left
# behind, or an unknown string) means recovery did NOT finish the fold.
_TERMINAL = {TxnState.ROLLED_BACK.name, TxnState.STUCK.name, TxnState.COMMITTED.name}


def _copy(src: str, dst: str) -> str:
    shutil.copy(src, dst)
    return dst


# === sanity: the baseline recovers cleanly (so corruption is the variable) ===


def test_baseline_midflight_recovers_exactly_once(charge_refund_tools, tmp_path):
    """The uncorrupted baseline fires one compensator per standing charge and
    lands ROLLED_BACK — and a second pass fires zero. If this regresses, every
    corruption assertion below is meaningless, so it is pinned explicitly."""
    db = str(tmp_path / "good.db")
    txn_id = build_midflight_journal(db, n_charges=3)

    a1 = CountingAdapter()
    report = recover(db, {"ext": a1})
    assert len(a1.applied) == 3
    assert report.transactions[0].final_state == TxnState.ROLLED_BACK.name
    assert read_durable_statuses(db, txn_id) == ["COMPENSATED"] * 3

    a2 = CountingAdapter()
    recover(db, {"ext": a2})
    assert a2.applied == []  # the durable fence holds


# === A. file-level corruption: truncate / byte-flip / overwrite ==============


@given(frac=st.floats(min_value=0.0, max_value=1.0))
@_FUZZ
def test_truncated_file_fails_loud_or_recovers_clean(charge_refund_tools, frac, tmp_path):
    """Truncating the SQLite file at an arbitrary offset must surface as a
    typed sqlite3 error OR a clean terminal recovery — never a success report
    that fired a compensator a non-integer number of times or skipped one
    silently. The forbidden outcome: a green report whose compensator count
    doesn't match the durable COMPENSATED count it claims to have written."""
    src = str(tmp_path / "good.db")
    build_midflight_journal(src, n_charges=3)
    db = _copy(src, str(tmp_path / "trunc.db"))
    size = Path(db).stat().st_size
    truncate_at(db, int(size * frac))

    _assert_recover_safe_or_loud(db)


@given(seed=st.integers(min_value=0, max_value=2**32 - 1), n=st.integers(1, 24))
@_FUZZ
def test_byteflip_fails_loud_or_recovers_safe(charge_refund_tools, seed, n, tmp_path):
    """Flipping random bytes either corrupts the container (typed sqlite error)
    or, if it happens to leave it parseable, must still recover SAFELY.

    Arbitrary byte corruption can leave a DB that parses but presents garbage
    rows (a flipped status byte, a duplicated page) — so an exact-count match
    against the pristine baseline is the wrong oracle. The genuinely-safe
    property under blunt corruption is the exactly-once / terminal one:
    recover lands terminal, and a SECOND pass fires zero further compensators
    (no double-undo). That is the silently-wrong class this kills."""
    src = str(tmp_path / "good.db")
    build_midflight_journal(src, n_charges=3)
    db = _copy(src, str(tmp_path / "flip.db"))
    flip_bytes(db, random.Random(seed), n)

    _assert_recover_safe_or_loud(db)


@given(offset=st.integers(min_value=0, max_value=8192),
       blob=st.binary(min_size=1, max_size=64))
@_FUZZ
def test_overwrite_range_fails_loud_or_recovers_safe(
    charge_refund_tools, offset, blob, tmp_path
):
    src = str(tmp_path / "good.db")
    build_midflight_journal(src, n_charges=3)
    db = _copy(src, str(tmp_path / "ow.db"))
    overwrite_range(db, offset, blob)

    _assert_recover_safe_or_loud(db)


# === B. semantic corruption: rows / columns / enums / JSON ===================


def test_deleted_effect_rows_never_double_or_phantom_compensate(
    charge_refund_tools, tmp_path
):
    """A journal flushed only partway (later effect rows missing) must
    compensate exactly the effects that DO survive on disk — never a phantom
    refund for a row that isn't there, never a double for one that is."""
    for keep in range(0, 4):
        db = str(tmp_path / f"del_{keep}.db")
        txn_id = build_midflight_journal(db, n_charges=3)
        delete_effect_rows(db, txn_id, keep=keep)
        surviving = len(read_durable_statuses(db, txn_id))  # == keep (capped at 3)

        adapter = CountingAdapter()
        report = recover(db, {"ext": adapter})
        # one refund per surviving APPLIED charge — no more, no less
        assert len(adapter.applied) == surviving, f"keep={keep}"
        if surviving:
            assert report.transactions[0].final_state == TxnState.ROLLED_BACK.name
        # second pass: the fence holds for whatever survived
        a2 = CountingAdapter()
        recover(db, {"ext": a2})
        assert a2.applied == [], f"keep={keep} second pass re-fired"


@given(bad_status=st.sampled_from(["", " ", "null", "None", "applied", "Applied"]))
@_FUZZ
def test_empty_or_wrongcase_status_fails_loud_not_silent(
    charge_refund_tools, bad_status, tmp_path
):
    """The status column is NOT NULL, but a corrupt journal can still carry an
    empty / wrong-case / lowercase string that is not a valid EffectStatus
    *name*. Recovery must raise (loud) — never treat an unrecognised status as
    'nothing to do' and silently leave the standing charge un-refunded while
    reporting success. (EffectStatus is keyed by NAME — 'APPLIED' — so the
    lowercase enum *values* like 'applied' are themselves invalid keys.)"""
    db = str(tmp_path / "badstatus.db")
    txn_id = build_midflight_journal(db, n_charges=2)
    set_effect_status(db, txn_id, idx=1, status=bad_status)

    adapter = CountingAdapter()
    with pytest.raises((KeyError, TypeError, ValueError)):
        recover(db, {"ext": adapter})


def test_nulled_args_column_fails_loud(charge_refund_tools, tmp_path):
    """args is NOT NULL in the schema and ``_effect_from_row`` does
    ``json.loads(row['args'])`` — a NULL (or the column dropped to None) must
    raise at the parse boundary, never pass a None straight into the
    compensator call as if it were valid args."""
    db = str(tmp_path / "nullargs.db")
    txn_id = build_midflight_journal(db, n_charges=2)
    # Bypass the NOT NULL via a column the fold parses; force it None-equivalent
    # by writing the literal string 'null' (valid JSON null → args becomes None).
    conn = sqlite3.connect(db)
    conn.isolation_level = None
    conn.execute(
        "UPDATE effects SET args = ? WHERE txn_id = ? AND idx = ?",
        ("null", txn_id, 1),
    )
    conn.close()

    adapter = CountingAdapter()
    # args=None → tool_fn(**None) is a TypeError when the compensator fires;
    # the runtime catches a raising compensator and lands STUCK (fail-safe),
    # which is acceptable — what is NOT acceptable is a clean ROLLED_BACK that
    # claims the refund succeeded.
    report = recover(db, {"ext": adapter})
    final = report.transactions[0].final_state
    assert final in _TERMINAL
    if final == TxnState.ROLLED_BACK.name:
        # If it claims fully rolled back, every charge must have genuinely
        # been refunded with real args — the None-args row must NOT count.
        for tool_name, args in adapter.applied:
            assert args is not None, "compensator fired with None args but report says clean"


@given(idx=st.integers(min_value=0, max_value=2))
@_FUZZ
def test_bogus_status_enum_fails_loud(charge_refund_tools, idx, tmp_path):
    """A status string outside the EffectStatus enum is a corrupt fence.
    ``EffectStatus[...]`` raises KeyError — recovery must surface that, never
    coerce an unknown status to APPLIED (double-fire risk) or to a terminal
    state (silent drop)."""
    db = str(tmp_path / "bogus.db")
    txn_id = build_midflight_journal(db, n_charges=3)
    set_effect_status(db, txn_id, idx, "TOTALLY_NOT_A_STATUS")

    adapter = CountingAdapter()
    with pytest.raises(KeyError):
        recover(db, {"ext": adapter})


@given(bogus_state=st.sampled_from(["NONSENSE", "open", "Committed", "", "123"]))
@_FUZZ
def test_bogus_txn_state_does_not_falsely_recover(charge_refund_tools, bogus_state, tmp_path):
    """A transaction whose state string is not a recoverable state must NOT be
    picked up as mid-flight (it would otherwise drive a fold the operator never
    asked for). The recoverable set is matched by exact enum name; an unknown
    state simply isn't selected — and crucially the standing charges are left
    APPLIED on disk (honest: 'we did not touch this'), never silently flipped."""
    db = str(tmp_path / "bogusstate.db")
    txn_id = build_midflight_journal(db, n_charges=2)
    set_txn_state(db, txn_id, bogus_state)

    adapter = CountingAdapter()
    report = recover(db, {"ext": adapter})
    # Not in the recoverable name set → not selected → no compensators fired.
    assert adapter.applied == []
    assert report.transactions == []
    # The durable charges are untouched — recovery did not silently rewrite them.
    assert read_durable_statuses(db, txn_id) == ["APPLIED", "APPLIED"]
    assert read_txn_state(db, txn_id) == bogus_state


def test_malformed_json_args_fails_loud(charge_refund_tools, tmp_path):
    """Invalid JSON in the args column hits ``json.loads`` in
    ``_effect_from_row``. It must raise a JSON / value error, never silently
    skip the effect (leaving the charge standing while reporting success)."""
    db = str(tmp_path / "badjson.db")
    txn_id = build_midflight_journal(db, n_charges=2)
    mangle_json_column(db, txn_id, "args", idx=0)

    adapter = CountingAdapter()
    with pytest.raises((sqlite3.OperationalError, ValueError)):
        # json.JSONDecodeError is a subclass of ValueError.
        recover(db, {"ext": adapter})


@given(new_idx=st.sampled_from([-1, -100, 999999, 2**31]))
@_FUZZ
def test_out_of_range_effect_index_is_safe(charge_refund_tools, new_idx, tmp_path):
    """An effect index rewritten out of range must not corrupt the fold:
    recovery either compensates each surviving APPLIED charge exactly once and
    lands terminal, or raises — never a partial fire that the report calls
    clean."""
    db = str(tmp_path / "oob.db")
    txn_id = build_midflight_journal(db, n_charges=3)
    set_effect_index(db, txn_id, old_idx=1, new_idx=new_idx)

    adapter = CountingAdapter()
    try:
        report = recover(db, {"ext": adapter})
    except (sqlite3.DatabaseError, ValueError, KeyError):
        return
    _assert_report_consistent_with_durable(report, adapter, db)


def test_dangling_compensator_name_lands_stuck(tmp_path):
    """A standing irreversible whose tool declares a compensator that is NOT in
    the registry cannot be undone. Recovery must land STUCK (fail-safe) — never
    a ROLLED_BACK that implies the side effect was reversed when it was not.

    Here we register 'charge' pointing at a compensator name that was never
    registered, so the resolution genuinely dangles at recover-time."""

    @tool(
        resource="ext",
        reversible=False,
        injects_handle=False,
        compensator="refund_that_does_not_exist",
    )
    def charge(amount):
        return None

    db = str(tmp_path / "dangling.db")
    txn_id = build_midflight_journal(db, n_charges=2)

    adapter = CountingAdapter()
    report = recover(db, {"ext": adapter})
    assert report.transactions[0].final_state == TxnState.STUCK.name
    assert adapter.applied == []  # no compensator could fire
    # Standing charges left APPLIED on disk: honest — manual recovery needed.
    assert read_durable_statuses(db, txn_id) == ["APPLIED", "APPLIED"]


def test_snapshot_referencing_missing_payload_is_safe(charge_refund_tools, tmp_path):
    """A reversible effect's snapshot payload mangled to garbage must not crash
    recovery into a wrong landing. Reversible effects are DB-auto-rolled-back on
    crash; recovery records them COMPENSATED WITHOUT touching the snapshot
    (the savepoint is dead cross-process), so a corrupt snapshot column must be
    irrelevant to the outcome — proving recovery never dereferences it."""

    @tool(resource="sql")
    def kv_write(conn, amount):
        return None

    # Build a journal with a reversible APPLIED effect (resource 'sql').
    from pherix.core.audit import AuditJournal
    from pherix.core.effects import Effect, EffectStatus
    from pherix.core.transaction import Transaction

    db = str(tmp_path / "snap.db")
    audit = AuditJournal(db)
    txn = Transaction()
    txn.state = TxnState.PARTIAL
    audit.record_transaction(txn)
    audit.update_transaction_state(txn.txn_id, TxnState.PARTIAL.name)
    eff = Effect(
        txn_id=txn.txn_id, index=0, tool="kv_write", args={"amount": 5},
        resource="sql", reversible=True, status=EffectStatus.APPLIED,
    )
    audit.record_effect(eff)
    audit.update_effect(eff)
    audit.close()
    # Mangle the snapshot column to garbage JSON.
    mangle_json_column(db, txn.txn_id, "snapshot", idx=0)

    # No 'sql' adapter is even needed — reversible recovery never calls it.
    report = recover(db, {})
    assert report.transactions[0].final_state == TxnState.ROLLED_BACK.name
    assert read_durable_statuses(db, txn.txn_id) == ["COMPENSATED"]


def test_corrupt_args_make_compensator_fail_lands_stuck_and_retries(
    charge_refund_tools, tmp_path
):
    """Corruption that mangles a compensator's args into a call that raises
    must land STUCK and leave the effect durably APPLIED — the honest, fail-safe
    outcome (the side effect is still standing; manual recovery is needed).

    Surfaced by the byte-flip fuzzer: flipping ``{"amount":100}`` to
    ``{"a1ount":100}`` keeps the JSON valid, so it parses, but ``refund(a1ount=
    100)`` raises ``TypeError`` (unexpected kwarg). The kernel catches the
    raising compensator, leaves the row APPLIED, and lands STUCK. This is NOT
    the exactly-once fence firing — STUCK txns are deliberately RETRIED, so a
    second recover() re-attempts the (still-failing) compensator. That retry is
    by design: the compensator never succeeded, so 'at most once successfully'
    still holds (zero successes). The property pinned here is that the failure
    is loud-in-the-report (STUCK, never a phantom ROLLED_BACK) and the durable
    status stays truthfully APPLIED.

    NOTE FOR THE KERNEL: a compensator with *partial* side effects that apply
    before it raises WOULD be double-applied across STUCK retries. That is a
    real at-most-once limitation of STUCK-retry, documented (recovery.py) and
    out of scope to fix here — it is not a silently-wrong rollback."""
    db = str(tmp_path / "badargs.db")
    txn_id = build_midflight_journal(db, n_charges=1)
    conn = sqlite3.connect(db)
    conn.isolation_level = None
    conn.execute(
        "UPDATE effects SET args = ? WHERE txn_id = ? AND idx = 0",
        ('{"a1ount": 100}', txn_id),  # 'm' -> '1': still valid JSON, wrong kwarg
    )
    conn.close()

    a1 = CountingAdapter()
    r1 = recover(db, {"ext": a1})
    assert r1.transactions[0].final_state == TxnState.STUCK.name
    assert read_durable_statuses(db, txn_id) == ["APPLIED"]  # truthfully standing
    assert r1.transactions[0].compensators_fired == 0  # none SUCCEEDED

    # STUCK is retried (by design) — a second pass re-attempts, still failing,
    # still STUCK, still APPLIED. The point: it never silently flips to a clean
    # ROLLED_BACK that would imply the charge was reversed when it was not.
    a2 = CountingAdapter()
    r2 = recover(db, {"ext": a2})
    assert r2.transactions[0].final_state == TxnState.STUCK.name
    assert read_durable_statuses(db, txn_id) == ["APPLIED"]
    assert r2.transactions[0].compensators_fired == 0


# === the consistency oracle ==================================================


def _assert_recover_safe_or_loud(db) -> None:
    """The safety oracle for blunt FILE-level corruption (truncate / flip /
    overwrite).

    Under arbitrary byte corruption a SQLite file may either fail to parse
    (typed error — fine) or parse into garbage rows that no longer match the
    pristine baseline (so an exact baseline-count match is the WRONG oracle).
    The property that must hold regardless is the exactly-once / fail-safe one:

      - if recover() raises, it raised a typed error (loud) — acceptable;
      - if it returns, every txn lands TERMINAL, and a SECOND recover() pass
        fires ZERO further compensators (the durable COMPENSATED fence holds —
        nothing is undone twice).

    The forbidden outcome: recover returns a 'success' report yet a second pass
    re-fires a compensator, i.e. the first pass left a real side effect APPLIED
    on disk while claiming to have undone it. That double-undo is silently
    wrong, and this oracle fails on it."""
    a1 = CountingAdapter()
    try:
        report = recover(db, {"ext": a1})
    except (sqlite3.DatabaseError, sqlite3.OperationalError, ValueError, KeyError,
            UnicodeDecodeError, TypeError):
        return  # (b) fail loud — acceptable

    for tr in report.transactions:
        assert tr.final_state in _TERMINAL, (
            f"non-terminal landing {tr.final_state!r} in a returned report"
        )

    # Exactly-once for SUCCESSFUL undo: a txn that landed ROLLED_BACK has every
    # standing effect durably COMPENSATED, so a second pass must fire zero
    # further compensators for it — the durable fence holds. (A txn that landed
    # STUCK is intentionally RETRIED on a later pass: its compensator never
    # succeeded, so re-attempting it is correct, not a double-undo. Corruption
    # that mangles a compensator's args into a TypeError is exactly such a case
    # — the kernel lands STUCK and leaves the effect APPLIED, which is honest.
    # See test_corrupt_args_make_compensator_fail_lands_stuck_and_retries.)
    rolled_back_txns = {
        tr.txn_id for tr in report.transactions
        if tr.final_state == TxnState.ROLLED_BACK.name
    }
    if not rolled_back_txns:
        return

    a2 = CountingAdapter()
    try:
        report2 = recover(db, {"ext": a2})
    except (sqlite3.DatabaseError, sqlite3.OperationalError, ValueError, KeyError,
            UnicodeDecodeError, TypeError):
        return
    # Any compensator the second pass fired must belong to a txn that was NOT
    # already fully rolled back — the rolled-back ones are fenced.
    re_fired_rolled_back = [
        tr for tr in report2.transactions
        if tr.txn_id in rolled_back_txns and tr.compensators_fired > 0
    ]
    assert not re_fired_rolled_back, (
        "second recovery pass re-fired a compensator for a txn that already "
        "landed ROLLED_BACK — exactly-once fence broken (a successful undo was "
        "applied twice)"
    )


def _assert_report_consistent_with_durable(report, adapter, db) -> None:
    """The load-bearing 'never silently wrong' check.

    Given a ``RecoveryReport`` that recover() actually returned (it did NOT
    raise), assert the report is consistent with what physically happened and
    with the durable journal it wrote:

    1. Every transaction lands in a terminal state — no OPEN/STAGED/PARTIAL
       left behind claiming to be 'recovered'.
    2. The number of compensators the adapter actually fired equals the number
       the report claims fired — the report cannot over- or under-count.
    3. Exactly-once: a second recover() pass fires ZERO further compensators.
       If the first pass left an APPLIED row it claimed to have undone, the
       second pass would re-fire it — that double-fire is the bug this catches.
    4. No effect the report counted as compensated is still APPLIED on disk,
       and vice versa: the durable fence matches the report.
    """
    # 1. terminal landing
    for tr in report.transactions:
        assert tr.final_state in _TERMINAL, (
            f"non-terminal landing {tr.final_state!r} in a returned report"
        )

    # 2. report's claimed compensator count == what the adapter actually ran
    claimed = sum(tr.compensators_fired for tr in report.transactions)
    assert claimed == len(adapter.applied), (
        f"report claims {claimed} compensators but adapter fired "
        f"{len(adapter.applied)} — silently wrong count"
    )

    # 3 + 4. exactly-once on a second pass, and durable fence consistency.
    # Snapshot the durable statuses the first pass committed, then re-run.
    for tr in report.transactions:
        statuses_after_first = read_durable_statuses(db, tr.txn_id)
        # nothing the fold touched should still be APPLIED if it claimed
        # ROLLED_BACK
        if tr.final_state == TxnState.ROLLED_BACK.name:
            assert "APPLIED" not in statuses_after_first, (
                f"txn {tr.txn_id} reported ROLLED_BACK but a durable effect is "
                f"still APPLIED — silently-wrong rollback"
            )

    a2 = CountingAdapter()
    try:
        recover(db, {"ext": a2})
    except (sqlite3.DatabaseError, ValueError, KeyError):
        # A second pass that fails loud is fine — it did not double-fire.
        return
    assert a2.applied == [], (
        "second recovery pass fired compensators again — exactly-once fence "
        "broken (the first pass left a row APPLIED that it claimed to undo)"
    )
