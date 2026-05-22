"""Shared scaffolding for the model-based state machine (``test_stateful_txn``).

Leading underscore keeps pytest from collecting it. It holds:

- the **reference model** — a dead-simple plain-Python mirror of what the world
  *should* be (committed kv dict + committed ledger dict, plus a working copy
  during an open txn). The model is the *oracle*: a divergence between it and
  the real adapter-backed world means a real kernel bug, not a model bug, so the
  model is kept deliberately trivial — no transactions, no versions, just dicts.
- thin builders for the **real worlds** the state machine drives: a fresh
  on-disk SQLite ``kv`` table behind the production :class:`SQLiteAdapter`
  (on-disk, not ``:memory:``, so crash+recover exercises the real
  "DB auto-rolled-back the uncommitted txn on process death" path and so the
  durable audit journal lives at a path :func:`recover` can reopen), and an
  in-memory ledger dict behind the irreversible :class:`LedgerAdapter`.

The kv tools and ledger tools are registered ONCE per machine *instance* (the
runtime forbids re-registration; ``tests/conftest.py`` clears the global
REGISTRY between tests). The mutable worlds (the SQLite file, the ledger dict)
are created fresh per machine run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# A tight key / account space keeps collisions frequent (insert-vs-overwrite,
# delete-present-vs-absent, repeat-charge-same-account) without blowing up the
# interleaving space — depth on a small domain.
KV_KEYS = ["a", "b", "c"]
LEDGER_ACCOUNTS = ["alice", "bob"]

_KV_DDL = "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v INTEGER NOT NULL)"


def fresh_kv_conn(path: str) -> sqlite3.Connection:
    """A fresh autocommit on-disk SQLite carrying an empty ``kv`` table.

    ``isolation_level=None`` hands every BEGIN / SAVEPOINT / COMMIT / ROLLBACK
    to the adapter — the mode :class:`SQLiteAdapter` requires. On-disk (not
    ``:memory:``) so a second connection opened by :func:`recover` after a
    simulated crash reads the same file.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(_KV_DDL)
    return conn


def dump_kv(conn: sqlite3.Connection) -> dict[str, int]:
    """The whole ``kv`` table as a plain dict — the comparable world-state."""
    return {k: v for k, v in conn.execute("SELECT k, v FROM kv")}


# --- the reference model: the oracle ----------------------------------------


@dataclass
class Model:
    """A plain-Python mirror of the committed world + the in-flight working set.

    Invariants the state machine checks are all of the form "real world ==
    model". The model never knows about savepoints, versions, journals — it is
    just two dicts (kv + ledger) that mutate exactly as a *correct* engine would
    leave the committed world. Keeping it this trivial is the whole point: the
    moment the real (production-machinery) world disagrees with this dict, the
    bug is in the kernel, not here.
    """

    # Committed world — what a SELECT / ledger-read should return when no txn is
    # open (or after a clean commit / rollback).
    kv_committed: dict[str, int] = field(default_factory=dict)
    ledger_committed: dict[str, int] = field(default_factory=dict)

    # Working copy while a txn is open. ``None`` ⇔ no open txn.
    kv_working: dict[str, int] | None = None
    # Pending (staged) ledger charges this txn will apply at commit:
    # account -> total charged. Mirrors what the ledger SHOULD gain on commit.
    pending_charges: dict[str, int] = field(default_factory=dict)
    # effect_id -> (account, amount) for every staged irreversible charge, so
    # the machine can pick one to approve and so commit can fold them forward.
    staged: dict[str, tuple[str, int]] = field(default_factory=dict)
    # effect_ids the machine has approved (or that are compensator-backed and
    # therefore auto-committable).
    approved: set[str] = field(default_factory=set)
    # effect_ids of staged charges with NO compensator (need explicit approval
    # or the gate blocks commit).
    needs_approval: set[str] = field(default_factory=set)

    @property
    def txn_open(self) -> bool:
        return self.kv_working is not None

    def open(self) -> None:
        """Begin a txn: snapshot the committed world into the working copy."""
        self.kv_working = dict(self.kv_committed)
        self.pending_charges = {}
        self.staged = {}
        self.approved = set()
        self.needs_approval = set()

    def kv_set(self, k: str, v: int) -> None:
        assert self.kv_working is not None
        self.kv_working[k] = v

    def kv_del(self, k: str) -> None:
        assert self.kv_working is not None
        self.kv_working.pop(k, None)

    def stage_charge(
        self, effect_id: str, account: str, amount: int, *, has_compensator: bool
    ) -> None:
        self.staged[effect_id] = (account, amount)
        self.pending_charges[account] = self.pending_charges.get(account, 0) + amount
        if has_compensator:
            self.approved.add(effect_id)  # auto-committable
        else:
            self.needs_approval.add(effect_id)

    def approve(self, effect_id: str) -> None:
        self.approved.add(effect_id)
        self.needs_approval.discard(effect_id)

    @property
    def gate_blocks(self) -> bool:
        """A commit blocks at the gate iff some staged charge is unapproved."""
        return bool(self.needs_approval - self.approved)

    def commit(self) -> None:
        """A clean commit: working copy becomes committed; charges land."""
        assert self.kv_working is not None
        self.kv_committed = dict(self.kv_working)
        for account, total in self.pending_charges.items():
            self.ledger_committed[account] = (
                self.ledger_committed.get(account, 0) + total
            )
        self._close()

    def rollback(self) -> None:
        """A rollback: working copy is dropped; committed unchanged. Staged
        irreversibles never fired, so the ledger is untouched."""
        self._close()

    def _close(self) -> None:
        self.kv_working = None
        self.pending_charges = {}
        self.staged = {}
        self.approved = set()
        self.needs_approval = set()


def ledger_equal(a: dict[str, int], b: dict[str, int]) -> bool:
    """Semantic equality on ledgers: a zero balance equals an absent account.

    ``refund ∘ charge`` leaves an account at balance ``0`` rather than removing
    the key, so byte-identity is the wrong test — the *meaning* is "no money
    moved". Same equality the compensator catalog uses for the left-inverse law.
    """
    accounts = set(a) | set(b)
    return all(a.get(acct, 0) == b.get(acct, 0) for acct in accounts)
