"""MemoryAdapter — governed agent memory as just another resource.

The north star says "governed memory" is **not a new axis**: an agent's memory
is a resource placed under the same envelope as any other — an *adapter + a
policy*. This module proves that by satisfying the existing
:class:`~pherix.core.adapters.base.ResourceAdapter` protocol against a durable
key/value memory store, with **no engine surgery**. ``remember`` / ``recall`` /
``forget`` become ordinary journalled effects: reversible, policy-governed,
audited, rollback-able — exactly like a SQL write or a file write.

Mechanism (a deliberate hybrid that makes it its own adapter, not a re-skin of
either neighbour):

- **Rollback is correct-by-construction via SQLite savepoints**, like
  :class:`~pherix.core.adapters.sql.SQLiteAdapter`: ``snapshot`` issues a real
  ``SAVEPOINT``, ``restore`` does ``ROLLBACK TO SAVEPOINT``, and the txn bracket
  is a plain ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``. The database does the undo —
  so a rolled-back ``remember`` simply never happened, and ``recall`` in a later
  transaction cannot see it.
- **Versioning is content-addressed**, like
  :class:`~pherix.core.adapters.filesystem.FilesystemAdapter`: a key's version
  is the sha256 of its current value (or the ``__missing__`` sentinel when
  absent), so the commit-time isolation diff flags "I read this memory, someone
  else rewrote it" without a counter side-table.

Durability comes from the SQLite file persisting committed state across runs and
processes — open a fresh adapter on the same path and ``recall`` returns what a
prior transaction committed.

The connection must be opened in autocommit mode (``isolation_level=None``) so
this adapter — not sqlite3's implicit machinery — owns every BEGIN / SAVEPOINT /
COMMIT / ROLLBACK, exactly as the SQL adapter requires.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.effects import Effect

# The single durable store table. ``namespace`` scopes one agent's memory from
# another's; ``mem_key`` is the lookup key within a namespace. ``value`` is the
# remembered payload (JSON text). One adapter instance binds one namespace.
_MEMORY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS _pherix_memory (
    namespace TEXT NOT NULL,
    mem_key   TEXT NOT NULL,
    value     TEXT NOT NULL,
    ts        TEXT NOT NULL,
    PRIMARY KEY (namespace, mem_key)
)
"""

# Sentinel returned by ``read_version`` for a key that has never been
# remembered. A non-None marker means the commit-time isolation diff can tell
# "I recalled this as absent" apart from a real content hash via a plain ``!=``
# — a later ``remember`` of the same key then correctly flags a conflict.
_MEM_MISSING = "__missing__"


class MemoryHandle:
    """The per-effect memory handle injected as the first arg of memory tools.

    The ``@tool`` wrapper hides it from the agent's call-site (D2), exactly as
    the SQL ``conn`` and the filesystem :class:`FsHandle` are hidden. It speaks
    the memory vocabulary — :meth:`remember` / :meth:`recall` / :meth:`forget` —
    and records read/write keys into the active :class:`Effect` so isolation and
    audit fall out for free. Recording is a no-op when ``effect`` is ``None``
    (the handle still works for raw unit tests outside ``agent_txn``).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        namespace: str,
        effect: Any = None,
        adapter: "MemoryAdapter | None" = None,
    ):
        self._conn = conn
        self._namespace = namespace
        self._effect = effect
        self._adapter = adapter
        self._recorded_reads: set[str] = set()

    # --- public API (tool-facing) -------------------------------------------

    def remember(self, key: str, value: Any) -> None:
        """Persist ``value`` under ``key`` (UPSERT). A journalled write."""
        payload = value if isinstance(value, str) else json.dumps(value)
        self._conn.execute(
            "INSERT INTO _pherix_memory (namespace, mem_key, value, ts) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(namespace, mem_key) DO UPDATE SET "
            "value = excluded.value, ts = excluded.ts",
            (self._namespace, key, payload, _now()),
        )
        self._record_write_key(key)

    def recall(self, key: str) -> Any:
        """Return the value remembered under ``key``, or ``None`` if absent.

        A read: it records a read_key but never a write_key, so a memory policy
        that forbids writes leaves ``recall`` untouched — recall is read-only by
        construction, not by a special rule.
        """
        row = self._conn.execute(
            "SELECT value FROM _pherix_memory WHERE namespace = ? AND mem_key = ?",
            (self._namespace, key),
        ).fetchone()
        self._record_read_key(key)
        return None if row is None else row[0]

    def forget(self, key: str) -> None:
        """Delete ``key`` from memory. A journalled write; absent key is a no-op."""
        self._conn.execute(
            "DELETE FROM _pherix_memory WHERE namespace = ? AND mem_key = ?",
            (self._namespace, key),
        )
        self._record_write_key(key)

    # --- isolation recording ------------------------------------------------

    def _record_read_key(self, key: str) -> None:
        if self._effect is None or self._adapter is None:
            return
        if key in self._recorded_reads:
            return
        version = self._adapter.read_version((key,))
        self._effect.read_keys.append(("memory", (key,), version))
        self._recorded_reads.add(key)

    def _record_write_key(self, key: str) -> None:
        # Re-hash AFTER the write lands so the recorded version is the one
        # ``read_version`` would report now — the commit-time diff's "expected
        # current" for this key. Writes are not deduplicated; the diff folds to
        # the freshest via ``last_my_write``.
        if self._effect is None or self._adapter is None:
            return
        version_after = self._adapter.read_version((key,))
        self._effect.write_keys.append(("memory", (key,), version_after))


class MemoryAdapter:
    """``ResourceAdapter`` over a durable, namespaced key/value memory store."""

    name = "memory"

    def __init__(self, conn: sqlite3.Connection, *, namespace: str = "default"):
        self._conn = conn
        self._namespace = namespace
        # Idempotent DDL — re-binding the adapter to the same file is safe.
        self._conn.execute(_MEMORY_TABLE_DDL)

    @property
    def conn(self) -> sqlite3.Connection:
        # Exposed so the existing ``sql_reader`` mediator can serve
        # ``ctx.read("memory", ...)`` world-state rules through the unchanged
        # runtime — the policy axis (incl. #7) covers memory with no new wiring.
        #
        # Honesty caveat (committed-only vs read-your-writes): this is the main
        # connection, which sits inside the txn's ``BEGIN`` while it is open. So
        # a commit-time world-state read of a key THIS txn has already written
        # sees its own uncommitted value (read-your-writes), not committed-only
        # state. That is correct for predicates over keys the txn does not write
        # (a lock marker, a sibling record); a rule whose predicate reads the
        # very key it writes will not observe a commit-time divergence from
        # stage-time. The SQL adapter's ``meta_conn`` (committed-only reads for
        # cross-process TOCTOU) is deliberately not replicated here — the memory
        # base is single-process; a design partner needing committed-only memory
        # reads pulls that in, exactly as #8 was pulled for SQL.
        return self._conn

    @property
    def namespace(self) -> str:
        return self._namespace

    def supports_rollback(self) -> bool:
        return True

    # --- transaction-scope lifecycle (TransactionalResourceAdapter) ---------

    def begin(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # --- per-effect snapshot / apply / restore ------------------------------

    @staticmethod
    def _savepoint_name(index: int) -> str:
        # ``index`` is a runtime-assigned integer, never user input, so building
        # the identifier by interpolation is safe (SQLite cannot parameterise
        # identifiers regardless).
        return f"mem_sp_{int(index)}"

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        sp = self._savepoint_name(effect.index)
        self._conn.execute(f"SAVEPOINT {sp}")
        return SnapshotHandle(
            resource=self.name,
            effect_index=effect.index,
            payload={"savepoint": sp},
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # D2: the txn-owned handle is injected as the tool's first arg; the
        # @tool wrapper hides it from the agent's call-site.
        from pherix.core.tools import active_effect

        handle = MemoryHandle(
            conn=self._conn,
            namespace=self._namespace,
            effect=active_effect.get(),
            adapter=self,
        )
        return tool_fn(handle, **effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        sp = handle.payload["savepoint"]
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")

    # --- versioning (content-addressed, like the filesystem adapter) --------

    def read_version(self, key: tuple) -> str:
        if len(key) != 1:
            raise ValueError(
                f"MemoryAdapter version key must be a 1-tuple (mem_key,); "
                f"got {key!r}"
            )
        row = self._conn.execute(
            "SELECT value FROM _pherix_memory WHERE namespace = ? AND mem_key = ?",
            (self._namespace, key[0]),
        ).fetchone()
        if row is None:
            return _MEM_MISSING
        return hashlib.sha256(row[0].encode("utf-8")).hexdigest()

    def write_version(self, key: tuple) -> str:
        # Recomputed from the stored value after the write — no cache.
        return self.read_version(key)

    # --- state diff (StateDiffable — dry-run structural delta) --------------

    def _dump(self) -> dict:
        """``{mem_key: value}`` for this namespace — a read-only snapshot."""
        rows = self._conn.execute(
            "SELECT mem_key, value FROM _pherix_memory WHERE namespace = ?",
            (self._namespace,),
        ).fetchall()
        return {k: v for k, v in rows}

    def state_baseline(self) -> dict:
        return self._dump()

    def state_diff(self, baseline: dict) -> dict:
        """Diff live memory against ``baseline`` into added/modified/deleted.

        Memory's structural answer to "what would this transaction remember or
        forget?" — the same shape a SQL/FS diff gives, keyed by ``mem_key`` so a
        front-end can attribute the change without re-deriving it.
        """
        now = self._dump()
        added = [k for k in now if k not in baseline]
        modified = [
            k for k, v in now.items() if k in baseline and baseline[k] != v
        ]
        deleted = [k for k in baseline if k not in now]
        return {
            "keys_added": added,
            "keys_modified": modified,
            "keys_deleted": deleted,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
