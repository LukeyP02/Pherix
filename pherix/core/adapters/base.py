"""The ResourceAdapter protocol — the seam that makes Pherix a system.

An adapter makes journal entries executable and reversible against a *class* of
real resource via ``snapshot -> apply -> restore``. ``core/adapters/`` never
imports ``core/tools.py``: the runtime resolves ``effect.tool`` to a callable and
hands it to ``apply`` as ``tool_fn``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pherix.core.effects import Effect


@dataclass
class SnapshotHandle:
    """Opaque handle to a captured before-state, returned by ``adapter.snapshot``.

    ``payload`` holds adapter-private, JSON-serialisable detail (e.g. the SQLite
    savepoint name) so the audit journal can persist it without special-casing.
    """

    resource: str
    effect_index: int
    payload: dict = field(default_factory=dict)


@runtime_checkable
class ResourceAdapter(Protocol):
    name: str

    def supports_rollback(self) -> bool:
        """Honesty flag — whether this resource can actually be restored."""
        ...

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        """Capture the before-state for ``effect``, prior to applying it."""
        ...

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        """Execute the effect by invoking the resolved tool callable."""
        ...

    def restore(self, handle: SnapshotHandle) -> None:
        """Restore the resource to the state captured by ``handle``."""
        ...


@runtime_checkable
class TransactionalResourceAdapter(ResourceAdapter, Protocol):
    """Adapters that carry a transaction-scope lifecycle (D1).

    Some resources need bracketing around the whole transaction — opening a
    BEGIN, allocating a per-txn workspace, releasing it on commit/rollback.
    Others (e.g. an irreversible HTTP adapter) have nothing to do at txn
    boundaries; they conform only to :class:`ResourceAdapter`. The runtime
    dispatches lifecycle calls by ``isinstance`` against this sub-protocol so
    a typo'd ``begin`` no longer silently skips, and the type system reflects
    the real taxonomy of resources.
    """

    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@runtime_checkable
class StateDiffable(ResourceAdapter, Protocol):
    """Adapters that can describe a transaction's structural effect (Slice 8).

    Slice 7's :class:`~pherix.core.dry_run.DryRunResult` carries the
    *journal* — the per-effect record of *intent*. Slice 8 adds the
    *structural* answer a gateway client wants: "which rows would have been
    inserted? which files would have been written?". That answer is a diff
    of the resource's current state against a baseline captured at
    transaction begin.

    Two methods, captured-then-compared:

    - :meth:`state_baseline` is called once at transaction begin — it
      returns an opaque, JSON-light snapshot of the *queryable* resource
      state (for SQL: ``{table: {pk: row}}`` over user tables; for FS:
      ``{relpath: sha256}`` over the rooted tree). This is **not** the
      per-effect ``snapshot`` — a SQLite ``SAVEPOINT`` is not separately
      queryable as a before-image, so the diff cannot read the pre-image
      from inside it. The baseline is a parallel, read-only capture that
      never touches the snapshot/apply/restore lane.
    - :meth:`state_diff` is called at the dry-run finalise hook, *before*
      the rollback discards the world, and compares the live resource
      against ``baseline``.

    Required output shapes (the cross-driver contract — front-ends assert
    on these keys, so do not rename them):

    - SQL: ``{"rows_added": [...], "rows_modified": [...],
      "rows_deleted": [...]}``
    - FS:  ``{"files_added": [...], "files_modified": [...],
      "files_deleted": [...]}``

    Adapters whose "diff" is not a state comparison do not conform.
    :class:`~pherix.core.adapters.http.HTTPAdapter` is the example: it is
    irreversible, has no queryable state to baseline, and its structural
    record is the journal's ``would_have_fired`` list — not a row/file
    delta. It deliberately does **not** implement these methods.
    """

    def state_baseline(self) -> Any:
        """Capture a read-only baseline of the resource at txn begin."""
        ...

    def state_diff(self, baseline: Any) -> dict:
        """Diff the live resource against ``baseline``; return the delta dict."""
        ...


class VersionedResourceAdapter(ResourceAdapter, Protocol):
    """Adapters that participate in isolation (Slice 4).

    Versions form a totally ordered tag space per key (monotonically
    increasing for SQL counters, or content-addressed sha256 for the
    filesystem). The commit-time isolation diff folds the journal:
    for every read effect, it re-reads the version *now* and compares
    against the version captured at read-time — a mismatch flags a
    conflict.

    Adapters that cannot honestly version their resource — e.g.
    :class:`HTTPAdapter`, which is irreversible and *isolated by
    construction* via the staging lane (irreversible effects defer
    fire to commit, so two pre-commit stages of the same effect
    cannot race) — do not conform.

    This Protocol is intentionally **not** ``@runtime_checkable``.
    ``isinstance`` against it would only check method presence, not
    behavioural conformance. The runtime gates isolation work on
    ``adapter.supports_rollback()`` instead — that is the honest
    contract. The Protocol exists for typing and documentation.
    """

    def read_version(self, key: tuple) -> object:
        """Return the current version tag for ``key``.

        Returns a non-None sentinel when the key has never been
        written / does not exist (``0`` for SQL counters, the literal
        string ``"__missing__"`` for filesystem hashes). Using a
        non-None sentinel means an "I read this as absent, then
        someone created it" case correctly flags as a conflict at
        commit time (sentinel != hash).
        """
        ...

    def write_version(self, key: tuple) -> object:
        """Bump (or recompute) the version tag for ``key`` and return it.

        For SQL: monotonic counter bump (atomic UPSERT). For filesystem:
        the sha256 of the on-disk content *after* the write.
        """
        ...


class SavepointAdapter:
    """Shared base for adapters whose per-effect undo is a SQLite ``SAVEPOINT``.

    Both :class:`~pherix.core.adapters.sql.SQLiteAdapter` and
    :class:`~pherix.core.adapters.memory.MemoryAdapter` back rollback on a real
    SQLite savepoint: ``snapshot`` issues ``SAVEPOINT``, ``restore`` runs
    ``ROLLBACK TO SAVEPOINT``, and the transaction bracket is a plain
    ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``. The database does the undo, so the
    reversible lane is correct by construction — this base holds that shared
    machinery exactly once. The connection must be opened in autocommit mode
    (``isolation_level=None``) so the adapter — not sqlite3's implicit
    machinery — owns every BEGIN / SAVEPOINT / COMMIT / ROLLBACK.

    Subclasses set :attr:`name` and :attr:`_SAVEPOINT_PREFIX`, and supply their
    own ``apply`` (how the journalled tool is invoked) and versioning — the two
    places SQL and memory genuinely differ. Postgres/MySQL deliberately do
    **not** inherit this: their drivers execute through a ``cursor()`` context
    manager — a different shape — so they keep their own savepoint methods
    rather than forcing a driver-execution abstraction into this base. That is
    the convergence test applied honestly: dedupe what is identical, do not
    over-generalise what is not.
    """

    name: str = ""
    # Distinguishes savepoint identifiers per resource class so two adapters
    # sharing one connection cannot collide on a savepoint name. The SQL family
    # uses ``sp``; memory uses ``mem_sp``.
    _SAVEPOINT_PREFIX: str = "sp"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (TransactionalResourceAdapter) ---------

    def begin(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # --- per-effect snapshot / restore --------------------------------------

    @classmethod
    def _savepoint_name(cls, index: int) -> str:
        # SQLite cannot parameterise identifiers; ``index`` is a runtime-assigned
        # integer (never user input), so interpolation is safe by construction.
        # A classmethod (not staticmethod) so the per-class prefix is read off
        # ``cls`` while staying callable on the class itself, e.g.
        # ``SQLiteAdapter._savepoint_name(5) == "sp_5"``.
        return f"{cls._SAVEPOINT_PREFIX}_{int(index)}"

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        sp = self._savepoint_name(effect.index)
        self._conn.execute(f"SAVEPOINT {sp}")
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"savepoint": sp},
        )

    def restore(self, handle: SnapshotHandle) -> None:
        sp = handle.payload["savepoint"]
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
