"""Shared Hypothesis generators for the trust-laws headline theorems.

This module is **not** a test module — the leading underscore keeps pytest
from collecting it. It sits on top of :mod:`tests._laws` and supplies the
*mixed* reversible-plus-irreversible action sequences the oversight gate-fuzz
and the audit-completeness theorems fold over.

The same discipline as ``tests._laws`` applies: a *fixed* toolset is
registered once per test function (Hypothesis re-runs the body many times
against the SAME registry, and re-registering a tool raises), so these
strategies generate **programs** — sequences of :class:`Action` describing
which already-registered tool to call with which args, and (for the
irreversible no-compensator kind) whether the operator approves it. The
mutable world (the SQLite connection, the call-log) is created fresh per
example inside the test body.

The action alphabet is exactly the one the oversight invariant ranges over:

- ``REV``        — a reversible SQL write (the snapshot/restore lane).
- ``IRR_COMP``   — an irreversible HTTP call that *has* a registered
                   compensator (the gate is satisfied automatically).
- ``IRR_NOCOMP`` — an irreversible HTTP call with *no* compensator; it can
                   only commit if explicitly approved. Each carries an
                   ``approve`` bit so the generator covers both the approved
                   and the un-approved branch.

An ``IRR_NOCOMP`` with ``approve=False`` anywhere in a program is what should
make the whole commit raise ``GateBlocked`` — that is the property the
gate-fuzz hammers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hypothesis import strategies as st

from tests._laws import KV_KEYS, LEDGER_ACCOUNTS


class ActionKind(Enum):
    """The three lanes a generated action can exercise."""

    REV = "rev"  # reversible SQL write
    IRR_COMP = "irr_comp"  # irreversible, compensator-backed
    IRR_NOCOMP = "irr_nocomp"  # irreversible, no compensator (needs approval)


@dataclass(frozen=True)
class Action:
    """One step in a generated mixed program.

    ``kind`` selects the lane; ``key`` / ``value`` carry the tool args
    (``key`` is a kv key for REV and an account name for the irreversible
    lanes; ``value`` is the integer written or the charge amount).
    ``approve`` is only meaningful for ``IRR_NOCOMP`` — it says whether the
    operator will call ``approve_irreversible`` for this effect before
    commit.
    """

    kind: ActionKind
    key: str
    value: int
    approve: bool = False


def _action_strategy() -> st.SearchStrategy:
    keys = st.sampled_from(KV_KEYS)
    accounts = st.sampled_from(LEDGER_ACCOUNTS)
    amounts = st.integers(min_value=1, max_value=10_000)
    values = st.integers(min_value=-1000, max_value=1000)

    rev = st.builds(
        Action,
        kind=st.just(ActionKind.REV),
        key=keys,
        value=values,
        approve=st.just(False),
    )
    irr_comp = st.builds(
        Action,
        kind=st.just(ActionKind.IRR_COMP),
        key=accounts,
        value=amounts,
        approve=st.just(False),
    )
    irr_nocomp = st.builds(
        Action,
        kind=st.just(ActionKind.IRR_NOCOMP),
        key=accounts,
        value=amounts,
        approve=st.booleans(),
    )
    return st.one_of(rev, irr_comp, irr_nocomp)


def mixed_programs(min_size: int = 0, max_size: int = 8) -> st.SearchStrategy:
    """Strategy: a list of :class:`Action` mixing all three lanes.

    The mix deliberately interleaves reversible writes with both flavours of
    irreversible call and with approve / no-approve choices, so the oversight
    invariant is tested under arbitrary orderings — the hostile interleavings
    the gate must survive.
    """
    return st.lists(
        _action_strategy(), min_size=min_size, max_size=max_size
    )
