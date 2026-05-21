"""Shared scaffolding for the kernel-law suites (``test_laws_*.py``).

This module is **not** a test module — the leading underscore keeps pytest from
collecting it. It holds the reusable "worlds" and Hypothesis strategies the law
suites fold random programs over.

The design rule for every law suite: a *fixed* toolset is registered once per
test function (Hypothesis re-runs the body many times against the SAME
registry, and re-registering a tool raises), while the *mutable world* — the
SQLite connection, the external ledger dict — is created fresh per example
inside the body. So the strategies here generate **programs** (sequences of
calls against an already-registered toolset), never new tool registrations.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from hypothesis import strategies as st

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect


# --- the reversible world: an in-memory SQLite key-value table ---------------
#
# We lean on the *real* SQLiteAdapter for the reversible laws rather than a
# hand-rolled fake: the law "rollback ≈ identity" is only meaningful if it is
# the production savepoint machinery being folded, not a toy whose own
# correctness we'd then have to trust.

KV_DDL = "CREATE TABLE kv (k TEXT PRIMARY KEY, v INTEGER NOT NULL)"


def fresh_kv_conn() -> sqlite3.Connection:
    """A fresh autocommit in-memory SQLite carrying an empty ``kv`` table.

    ``isolation_level=None`` hands every BEGIN/SAVEPOINT/COMMIT/ROLLBACK to the
    adapter — the same mode :class:`SQLiteAdapter` requires.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(KV_DDL)
    return conn


def dump_kv(conn: sqlite3.Connection) -> dict[str, int]:
    """The whole ``kv`` table as a plain dict — the comparable world-state."""
    return {k: v for k, v in conn.execute("SELECT k, v FROM kv")}


# A small key space keeps collisions (and thus update-vs-insert paths) frequent
# without blowing up the state space — depth of coverage on a tight domain.
KV_KEYS = ["a", "b", "c", "d"]


@dataclass(frozen=True)
class KvOp:
    """One reversible operation in a generated program. ``op`` ∈ {set, del}."""

    op: str
    key: str
    value: int = 0


def kv_programs(min_size: int = 0, max_size: int = 12) -> st.SearchStrategy:
    """Strategy: a list of :class:`KvOp` over the fixed key space.

    ``set`` writes/overwrites ``k -> v``; ``del`` removes ``k`` (a no-op if
    absent). The mix deliberately exercises insert, overwrite, delete-present
    and delete-absent so the rollback fold sees every shape.
    """
    keys = st.sampled_from(KV_KEYS)
    sets = st.builds(
        KvOp,
        op=st.just("set"),
        key=keys,
        value=st.integers(min_value=-1000, max_value=1000),
    )
    dels = st.builds(KvOp, op=st.just("del"), key=keys)
    return st.lists(st.one_of(sets, dels), min_size=min_size, max_size=max_size)


def seed_programs() -> st.SearchStrategy:
    """A pre-committed starting state: ``{key: value}`` over the key space.

    Used as the "before" world a transaction must restore to on rollback —
    proving rollback lands at an arbitrary committed baseline, not just empty.
    """
    return st.dictionaries(
        keys=st.sampled_from(KV_KEYS),
        values=st.integers(min_value=-1000, max_value=1000),
        max_size=len(KV_KEYS),
    )


# --- the irreversible world: an external "ledger" service --------------------
#
# Compensators undo what cannot be snapshotted, so the compensator laws need an
# irreversible adapter (``supports_rollback() -> False``) over a world Pherix
# cannot restore structurally — only a semantic inverse can move it back. We
# model a payment ledger: ``charge`` adds to a balance, its compensator
# ``refund`` subtracts the same amount. ``refund`` is invoked by the runtime
# with the original effect's args (see runtime._partial_unwind), so it is a
# true left-inverse: ``refund ∘ charge = identity`` on the ledger.


class LedgerAdapter:
    """Irreversible adapter over an in-memory external ledger (a ``dict``).

    Conforms to :class:`ResourceAdapter` only (no transaction lifecycle), like
    the real :class:`~pherix.core.adapters.http.HTTPAdapter`. The ledger dict
    is passed in so a test can inspect the "external world" before and after.
    """

    name = "ledger"

    def __init__(self, ledger: dict[str, int]):
        self.ledger = ledger

    def supports_rollback(self) -> bool:
        return False

    def snapshot(self, effect: Effect) -> SnapshotHandle:  # pragma: no cover
        raise AssertionError("irreversible adapter must never be snapshotted")

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # The ledger is injected as the tool's first arg (injects_handle=True),
        # so the call-site stays ``charge(account=..., amount=...)``.
        return tool_fn(self.ledger, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:  # pragma: no cover
        raise AssertionError("irreversible adapter has no before-state to restore")


def charge_impl(ledger: dict[str, int], account: str, amount: int) -> int:
    ledger[account] = ledger.get(account, 0) + amount
    return ledger[account]


def refund_impl(ledger: dict[str, int], account: str, amount: int) -> int:
    ledger[account] = ledger.get(account, 0) - amount
    return ledger[account]


def ledger_equal(a: dict[str, int], b: dict[str, int]) -> bool:
    """Semantic equality on ledgers: a zero balance equals an absent account.

    ``refund ∘ charge`` leaves an account at balance ``0`` rather than removing
    the key, so byte-identity of the dict is the wrong test — the *meaning* is
    "no money moved". This is the equality the catalog defines for the
    left-inverse law (the kernel never asserts the property; the catalog does).
    """
    accounts = set(a) | set(b)
    return all(a.get(acct, 0) == b.get(acct, 0) for acct in accounts)


LEDGER_ACCOUNTS = ["alice", "bob", "carol"]


@dataclass(frozen=True)
class Charge:
    account: str
    amount: int


def charge_programs(min_size: int = 0, max_size: int = 10) -> st.SearchStrategy:
    """Strategy: a list of :class:`Charge` over the fixed account space.

    Amounts are bounded and non-trivial; the law under test is that the sum of
    charges is exactly reversed by the matching refunds regardless of order,
    overlap, or repetition on the same account.
    """
    return st.lists(
        st.builds(
            Charge,
            account=st.sampled_from(LEDGER_ACCOUNTS),
            amount=st.integers(min_value=1, max_value=10_000),
        ),
        min_size=min_size,
        max_size=max_size,
    )


# --- a controllable reference adapter for differential testing ---------------
#
# A pure in-memory key-value adapter that drives the *same* runtime the SQLite
# adapter does, so a generated program folded through both must yield the same
# committed world and the same journal-status sequence. It tracks versions in
# the own-write-visible way (matching :memory: SQLite), so the isolation diff
# treats both identically.


@dataclass
class DictAdapter:
    """Reversible in-memory adapter — a reference oracle for SQLite semantics.

    Implements the full transactional + versioned + per-effect snapshot lane so
    the runtime drives it unchanged. State is a flat ``{key: value}`` dict;
    snapshots are deep-enough copies of that dict.
    """

    name: str = "kv"
    committed: dict[str, int] = field(default_factory=dict)
    _working: dict[str, int] | None = None
    _versions: dict[tuple, int] = field(default_factory=dict)
    _snaps: dict[int, dict[str, int]] = field(default_factory=dict)

    def supports_rollback(self) -> bool:
        return True

    # transaction-scope lifecycle
    def begin(self) -> None:
        self._working = dict(self.committed)

    def commit(self) -> None:
        if self._working is not None:
            self.committed = dict(self._working)
        self._working = None

    def rollback(self) -> None:
        self._working = None

    # per-effect snapshot / apply / restore
    def snapshot(self, effect: Effect) -> SnapshotHandle:
        self._snaps[effect.index] = dict(self._working or {})
        return SnapshotHandle(
            resource=self.name, effect_index=effect.index, payload={"i": effect.index}
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        return tool_fn(self, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        self._working = dict(self._snaps[handle.payload["i"]])

    # versioning (own-write-visible, like :memory: SQLite)
    def read_version(self, key: tuple) -> int:
        return self._versions.get(tuple(key), 0)

    def write_version(self, key: tuple) -> int:
        k = tuple(key)
        self._versions[k] = self._versions.get(k, 0) + 1
        return self._versions[k]

    # tool-facing handle API (mirrors what kv SQL tools do)
    def set(self, k: str, v: int) -> None:
        assert self._working is not None
        self._working[k] = v

    def delete(self, k: str) -> None:
        assert self._working is not None
        self._working.pop(k, None)

    def state(self) -> dict[str, int]:
        return dict(self.committed)
