"""MySQLAdapter — the reversible adapter for MySQL / MariaDB.

Same shape as :class:`~pherix.core.adapters.sql.SQLiteAdapter`: the database
does the heavy lifting. ``snapshot`` issues a real ``SAVEPOINT``, ``restore``
does ``ROLLBACK TO SAVEPOINT`` — correct by construction, so
``supports_rollback()`` is ``True``.

**Engine requirement.** Savepoints and transactional rollback require a
transactional storage engine — **InnoDB** (the MySQL default since 5.5). DDL in
MySQL is *not* transactional and implicitly commits, so the savepoint/restore
lane covers DML (rows), not schema changes — same as the SQLite and Postgres
adapters in practice.

**Connection contract.** ``__init__`` takes an already-open pymysql
``Connection``. The adapter drives every BEGIN / SAVEPOINT / COMMIT / ROLLBACK
*itself*, so the connection must be in **autocommit** mode — otherwise pymysql
opens an implicit transaction that fights this adapter's explicit one. Open it
as::

    import pymysql
    conn = pymysql.connect(host=..., user=..., database=...)
    conn.autocommit(True)
    adapter = MySQLAdapter(conn)

This mirrors SQLite's "the adapter controls every transaction boundary"
discipline (there it is ``isolation_level=None``; here it is
``conn.autocommit(True)``).

**Lazy driver import.** ``pymysql`` is imported inside ``__init__`` only, so
``import pherix`` works with zero third-party packages installed. The module
top level imports only stdlib + the adapter base + ``Effect``.

**Cross-process isolation.** Like :class:`PostgresAdapter` and unlike
:class:`SQLiteAdapter`, this adapter does NOT ship the ``_pherix_intents``
sibling-file intent ledger. That is a SQLite-single-host workaround for
SQLite's single-writer lock. InnoDB has real MVCC and row-level locking, so
cross-process isolation is delegated to the engine's read/write-set diff plus
the database's native locking — no Pherix-side intent ledger is needed.

**No RETURNING.** MySQL lacks ``INSERT ... RETURNING``, so
:meth:`MySQLAdapter.write_version` does the atomic upsert
(``INSERT ... ON DUPLICATE KEY UPDATE``) and then ``SELECT``\\ s the new value
back within the same transaction — still race-free because the row is locked by
the upsert until the txn boundary.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# Side-table holding monotonic version counters per (resource, key) — the
# isolation substrate (Slice 4). ``key_json`` is the canonical JSON encoding of
# the key tuple (see :meth:`MySQLAdapter._encode_key`). InnoDB is required for
# the transactional savepoint lane and is the default engine. Key columns use a
# bounded prefix length because TEXT cannot be a primary key in MySQL without
# one; VARCHAR(255) is ample for resource names and JSON-encoded keys here.
_VERSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _pherix_versions (
    resource VARCHAR(255) NOT NULL,
    key_json VARCHAR(255) NOT NULL,
    version  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, key_json)
) ENGINE=InnoDB
"""


class MySQLAdapter:
    name = "mysql"

    def __init__(self, conn: Any):
        # Lazy import: pymysql is an optional extra. Importing it here (not at
        # module top level) keeps ``import pherix`` dependency-free.
        import pymysql  # noqa: F401  (import for its install-check side effect)

        self._conn = conn
        # Create the version side-table eagerly so the first read_version on an
        # unknown key returns 0 (not a missing-table error). DDL is idempotent
        # so re-binding the adapter to the same DB is safe.
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
        # MySQL cannot parameterise identifiers. `index` is an internal,
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
        # Reads through the connection. InnoDB MVCC gives this read the latest
        # committed snapshot when outside an explicit txn, and this txn's own
        # bumps when inside one — sufficient for the commit-time diff. Absent
        # row → version 0 ("never written"); never returns None.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM _pherix_versions "
                "WHERE resource = %s AND key_json = %s",
                (self.name, self._encode_key(key)),
            )
            row = cur.fetchone()
        return 0 if row is None else int(row[0])

    def write_version(self, key: tuple) -> int:
        # MySQL has no RETURNING, so we do the atomic upsert then SELECT the
        # new value back. The upsert takes a row lock that is held until the
        # txn boundary, so the subsequent SELECT on the same connection cannot
        # observe an interleaved bump from another connection — keeping the
        # read-back race-free. Both statements run on the same connection in
        # the same transaction.
        resource = self.name
        key_json = self._encode_key(key)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO _pherix_versions (resource, key_json, version) "
                "VALUES (%s, %s, 1) "
                "ON DUPLICATE KEY UPDATE version = version + 1",
                (resource, key_json),
            )
            cur.execute(
                "SELECT version FROM _pherix_versions "
                "WHERE resource = %s AND key_json = %s",
                (resource, key_json),
            )
            row = cur.fetchone()
        return int(row[0])
