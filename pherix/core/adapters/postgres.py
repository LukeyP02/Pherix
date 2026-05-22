"""PostgresAdapter — the reversible adapter for PostgreSQL.

Same shape as :class:`~pherix.core.adapters.sql.SQLiteAdapter`: the database
does the heavy lifting. ``snapshot`` issues a real ``SAVEPOINT``, ``restore``
does ``ROLLBACK TO SAVEPOINT`` — correct by construction, so
``supports_rollback()`` is ``True``.

**Connection contract.** ``__init__`` takes an already-open psycopg
``Connection`` (psycopg 3). The adapter drives every BEGIN / SAVEPOINT /
COMMIT / ROLLBACK *itself*, so the connection must be in **autocommit** mode —
otherwise psycopg opens an implicit transaction that fights this adapter's
explicit one. Open it as::

    import psycopg
    conn = psycopg.connect("dbname=...")
    conn.autocommit = True
    adapter = PostgresAdapter(conn)

This mirrors SQLite's "the adapter controls every transaction boundary"
discipline (there it is ``isolation_level=None``; here it is
``conn.autocommit = True``).

**Lazy driver import.** ``psycopg`` is imported inside ``__init__`` only, so
``import pherix`` works with zero third-party packages installed. The module
top level imports only stdlib + the adapter base + ``Effect``.

**Cross-process isolation.** Unlike :class:`SQLiteAdapter`, this adapter does
NOT ship the ``_pherix_intents`` sibling-file intent ledger. That machinery is
a SQLite-single-host-specific hack working around SQLite's single-writer lock.
Postgres has real MVCC and row-level locking, so cross-process isolation is
delegated to the engine's existing read/write-set diff plus the database's
native locking — no Pherix-side intent ledger is needed.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# Side-table holding monotonic version counters per (resource, key) — the
# isolation substrate (Slice 4). ``key_json`` is the canonical JSON encoding of
# the key tuple (see :meth:`PostgresAdapter._encode_key`). Created idempotently
# on adapter init so the first ``read_version`` of an unknown key returns 0.
_VERSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _pherix_versions (
    resource TEXT NOT NULL,
    key_json TEXT NOT NULL,
    version  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, key_json)
)
"""


class PostgresAdapter:
    name = "postgres"

    def __init__(self, conn: Any):
        # Lazy import: psycopg is an optional extra. Importing it here (not at
        # module top level) keeps ``import pherix`` dependency-free. We do not
        # bind the imported module to an instance attr — we only need it to
        # have driven the import-error contract; the connection is supplied.
        import psycopg  # noqa: F401  (import for its install-check side effect)

        self._conn = conn
        # Create the version side-table eagerly so the first read_version on an
        # unknown key returns 0 (not a missing-table error). DDL is idempotent
        # so re-binding the adapter to the same DB is safe. Runs in autocommit,
        # so the table persists outside any agent_txn BEGIN this adapter drives.
        with self._conn.cursor() as cur:
            cur.execute(_VERSIONS_TABLE_DDL)

    @property
    def conn(self) -> Any:
        return self._conn

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (driven by the runtime, D3) ---

    def begin(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("BEGIN")

    def commit(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("COMMIT")

    def rollback(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("ROLLBACK")

    # --- per-effect snapshot / apply / restore ---

    @staticmethod
    def _savepoint_name(index: int) -> str:
        # Postgres cannot parameterise identifiers. `index` is an internal,
        # runtime-assigned integer (never user input), so building the
        # savepoint name by interpolation is safe by construction.
        return f"sp_{int(index)}"

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        sp = self._savepoint_name(effect.index)
        with self._conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {sp}")
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"savepoint": sp},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # D2: the txn-owned connection is injected as the tool's first arg;
        # the @tool wrapper hides it from the agent's call-site. SQL tools
        # declare injects_handle=True so the runtime routes here.
        return tool_fn(self._conn, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        sp = handle.payload["savepoint"]
        with self._conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")

    # --- versioning (Slice 4 — VersionedResourceAdapter) -------------------

    @staticmethod
    def _encode_key(key: tuple) -> str:
        # Pre-coercing the tuple to a list gives a stable JSON form regardless
        # of how json handles tuples. ``sort_keys`` is a no-op for lists but
        # keeps the encoding canonical if a key ever contains a nested dict.
        return json.dumps(list(key), sort_keys=True)

    def read_version(self, key: tuple) -> int:
        # Reads through the (autocommit) connection. Postgres MVCC gives this
        # read the latest committed snapshot when outside an explicit txn, and
        # this txn's own bumps when inside one — sufficient for the commit-time
        # diff. Absent row → version 0 ("never written"); never returns None.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM _pherix_versions "
                "WHERE resource = %s AND key_json = %s",
                (self.name, self._encode_key(key)),
            )
            row = cur.fetchone()
        return 0 if row is None else int(row[0])

    def write_version(self, key: tuple) -> int:
        # Atomic UPSERT with RETURNING so the bump is race-free against a
        # second connection to the same database — Postgres takes a row lock
        # for the INSERT ... ON CONFLICT, so concurrent bumps serialise.
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO _pherix_versions (resource, key_json, version) "
                "VALUES (%s, %s, 1) "
                "ON CONFLICT (resource, key_json) DO UPDATE "
                "SET version = _pherix_versions.version + 1 "
                "RETURNING version",
                (self.name, self._encode_key(key)),
            )
            row = cur.fetchone()
        return int(row[0])
