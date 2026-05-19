"""SQLiteAdapter — the reversible adapter, correct by construction.

The database does the heavy lifting: ``snapshot`` issues a real ``SAVEPOINT``,
``restore`` does ``ROLLBACK TO SAVEPOINT``. The connection must be opened in
autocommit mode (``isolation_level=None``) so this adapter — not sqlite3's
implicit machinery — controls every BEGIN / SAVEPOINT / COMMIT / ROLLBACK.

The :class:`SQLiteAdapter` class never imports ``core/tools.py`` at module
load: the runtime resolves ``effect.tool`` to a callable and passes it to
``apply`` as ``tool_fn``. Slice 4's :func:`execute_isolated` helper does
read the ``active_effect`` contextvar from ``core/tools.py``, but only via
a function-local import to keep module-load ordering free of cycles.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# Side-table holding monotonic version counters per (resource, key).
# ``key_json`` is ``json.dumps(list(key), sort_keys=True)`` so cross-process
# readers (multiple Python processes against the same SQLite file) see
# consistent rows. The table is created on adapter init.
_VERSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _pherix_versions (
    resource TEXT NOT NULL,
    key_json TEXT NOT NULL,
    version  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, key_json)
)
"""

# Seam used by the module-level :func:`execute_isolated` helper to find the
# :class:`SQLiteAdapter` bound to a given connection. We cannot attach
# attributes to a C-extension ``sqlite3.Connection`` directly (it forbids
# both attribute-set and weakref), so we key by ``id(conn)`` and require
# every adapter to register here on construction. The mapping leaks an
# entry per Connection object created in-process — a non-issue in test /
# agent-runtime contexts where adapters and connections are O(handful).
# A future revision could ship an explicit ``unregister`` if a long-lived
# process churns many connections.
_CONN_ADAPTERS: dict[int, "SQLiteAdapter"] = {}


def _adapter_for(conn: sqlite3.Connection) -> "SQLiteAdapter | None":
    """Return the :class:`SQLiteAdapter` registered for ``conn`` if any."""
    return _CONN_ADAPTERS.get(id(conn))


class SQLiteAdapter:
    name = "sql"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        # Slice 4: create the version side-table eagerly so the first
        # read_version on an unknown key returns 0 (not a missing-table
        # error). DDL is idempotent so re-binding the adapter is safe.
        self._conn.execute(_VERSIONS_TABLE_DDL)
        # Slice 4 seam: register this adapter against the connection's
        # identity so the module-level :func:`execute_isolated` helper can
        # find it from a bare ``sqlite3.Connection`` (SQL tools accept a
        # plain Connection — no adapter parameter pollutes the call-site).
        # We must use an id-keyed module dict because ``sqlite3.Connection``
        # is a C-extension type that forbids both attribute assignment and
        # weakrefs.
        _CONN_ADAPTERS[id(conn)] = self
        # Slice 4 (D5 multi-process arbitration): a separate autocommit
        # "meta" connection used only for ``read_version`` queries.
        # Rationale: during an ``agent_txn`` the main connection sits
        # inside an open ``BEGIN``. Under WAL mode (and even some non-WAL
        # cases) its reads of ``_pherix_versions`` are snapshot-isolated
        # to the moment the txn started — so a bump committed by another
        # process *while we are open* would be invisible to us, and the
        # commit-time diff would silently miss the cross-process
        # lost-update. The meta-connection is never inside a BEGIN; its
        # reads see the latest committed state. Writes still go through
        # the main connection so they roll back with the txn.
        # In-memory DBs (``:memory:``) have no shareable path; meta_conn
        # is None and ``read_version`` falls back to the main conn —
        # which is fine, in-memory is single-process by definition.
        db_path = self._derive_db_path(conn)
        self._meta_conn: sqlite3.Connection | None = (
            sqlite3.connect(db_path, isolation_level=None)
            if db_path
            else None
        )

    @staticmethod
    def _derive_db_path(conn: sqlite3.Connection) -> str | None:
        """Path of the connection's ``main`` database, or None for memory.

        ``PRAGMA database_list`` yields rows ``(seq, name, file)`` — file
        is an empty string for ``:memory:`` connections. We honour only
        the ``main`` schema; attached schemas are out of scope (Slice 4
        does not promise multi-schema isolation).
        """
        for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return file or None
        return None

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (driven by the runtime, D3) ---

    def begin(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # --- per-effect snapshot / apply / restore ---

    @staticmethod
    def _savepoint_name(index: int) -> str:
        # SQLite cannot parameterise identifiers. `index` is an internal,
        # runtime-assigned integer (never user input), so building the
        # savepoint name by interpolation is safe by construction.
        return f"sp_{int(index)}"

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        sp = self._savepoint_name(effect.index)
        self._conn.execute(f"SAVEPOINT {sp}")
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"savepoint": sp},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # D2: the txn-owned connection is injected as the tool's first arg;
        # the @tool wrapper hides it from the agent's call-site.
        return tool_fn(self._conn, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        sp = handle.payload["savepoint"]
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")

    # --- versioning (Slice 4 — VersionedResourceAdapter) -------------------

    @staticmethod
    def _encode_key(key: tuple) -> str:
        # Pre-coercing the tuple to a list gives a stable JSON form across
        # processes regardless of how json handles tuples. ``sort_keys`` is
        # a no-op for lists but keeps the encoding canonical if a key ever
        # contains a nested dict.
        return json.dumps(list(key), sort_keys=True)

    def read_version(self, key: tuple) -> int:
        # Use the meta-connection if available (D5: it bypasses the main
        # connection's BEGIN snapshot, so cross-process bumps are visible
        # at commit-time diff). Falls back to the main connection for
        # in-memory adapters where meta_conn does not exist.
        target = self._meta_conn or self._conn
        row = target.execute(
            "SELECT version FROM _pherix_versions "
            "WHERE resource = ? AND key_json = ?",
            (self.name, self._encode_key(key)),
        ).fetchone()
        # Absent row → version 0 ("never written"). Never returns None.
        return 0 if row is None else int(row[0])

    def write_version(self, key: tuple) -> int:
        # Atomic UPSERT with RETURNING so the bump is race-free against a
        # second connection to the same on-disk SQLite file.
        cur = self._conn.execute(
            "INSERT INTO _pherix_versions (resource, key_json, version) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(resource, key_json) DO UPDATE "
            "SET version = version + 1 "
            "RETURNING version",
            (self.name, self._encode_key(key)),
        )
        row = cur.fetchone()
        return int(row[0])


# --- Slice 4 isolation helper ---------------------------------------------


def execute_isolated(
    conn: sqlite3.Connection,
    stmt: str,
    params: tuple = (),
    reads: list[tuple] | None = None,
    writes: list[tuple] | None = None,
) -> sqlite3.Cursor:
    """SQL execution that records read/write keys into the active Effect.

    ``reads`` and ``writes`` are lists of ``(table, pk_value)`` tuples — the
    keys this statement reads from and writes to. SQL parsing is out of
    scope, so Slice 4 ships explicit key declaration: tools say which rows
    they touched. A future iteration could derive the keys from the
    statement itself; the journal shape is the same either way.

    Behaviour:

    - Always runs ``conn.execute(stmt, params)`` and returns the cursor.
    - Inside an ``agent_txn``, records each read as ``("sql", tuple(key),
      adapter.read_version(key))`` into ``active_effect.read_keys`` and
      each write as ``("sql", tuple(key))`` into ``write_keys``, after
      bumping the side-table version via ``adapter.write_version(key)``.
      Re-recording the same triple / pair within one effect is suppressed
      so a tool that touches the same row repeatedly doesn't bloat the
      journal. The version-side-table bump still fires on every write
      call — every statement is a real write that increments the version,
      even if its presence in ``write_keys`` is already noted.
    - Outside an ``agent_txn`` (``active_effect`` is ``None``), the stmt
      still runs but no recording happens — keeps the helper usable from
      raw unit tests of SQL tools.
    - If the connection has no registered adapter (a bare
      ``sqlite3.Connection`` not wrapped by :class:`SQLiteAdapter`),
      recording is also skipped. This is the same graceful-degrade as
      the no-effect case.
    """
    # Local import to keep ``sql.py`` module-load free of any dependency on
    # ``core/tools.py`` (and the contextvar living there).
    from pherix.core.tools import active_effect

    cursor = conn.execute(stmt, params or ())
    effect = active_effect.get()
    if effect is None:
        return cursor
    adapter = _adapter_for(conn)
    if adapter is None:
        return cursor
    for key in reads or ():
        key_t = tuple(key)
        v = adapter.read_version(key_t)
        triple = ("sql", key_t, v)
        if triple not in effect.read_keys:
            effect.read_keys.append(triple)
    for key in writes or ():
        key_t = tuple(key)
        # Slice 4 P3: write_keys carries `(resource, key, version_after_my_write)`
        # so the commit-time diff can disambiguate self-bumps from cross-txn
        # writes via `last_my_write` lookup. The third element is "my expected
        # current" for this key after my write — if the live version differs,
        # someone else also wrote this key during my txn. Writes are NOT
        # deduplicated: repeated writes append fresh triples and the diff
        # picks the freshest via iteration order.
        v_after = adapter.write_version(key_t)
        effect.write_keys.append(("sql", key_t, v_after))
    return cursor
