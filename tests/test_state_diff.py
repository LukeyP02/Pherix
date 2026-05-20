"""Slice 8 (Stream B): the ``StateDiffable`` sub-protocol + dry-run state diff.

Slice 7's :class:`DryRunResult` carried the *journal* — the per-effect record
of intent — but not a *structural* answer to "which rows would have been
inserted? which files would have been written?". That answer needs each
adapter to opt in: a ``StateDiffable`` adapter captures a lightweight baseline
of the resource at transaction begin, then computes a current-vs-baseline diff
at the dry-run finalise hook (before the rollback discards everything).

The diff is fully additive — it reads the live resource and the captured
baseline; it never touches the per-effect snapshot/apply/restore lane.

Required output shapes (the cross-stream contract):
  - SQL: ``{"rows_added": [...], "rows_modified": [...], "rows_deleted": [...]}``
  - FS:  ``{"files_added": [...], "files_modified": [...], "files_deleted": [...]}``
The runtime assembles a per-resource outer dict keyed by adapter name:
  ``state_diff = {"sql": {...}, "fs": {...}}``.

Tools are defined inside each test: the autouse ``_clean_tool_registry``
fixture clears the process-global registry around every test.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pherix.core.adapters.base import StateDiffable
from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.dry_run import dry_run
from pherix.core.tools import tool


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    yield c
    c.close()


def test_http_adapter_is_not_state_diffable() -> None:
    """HTTPAdapter's "diff" is would_have_fired; it does not opt in."""
    assert not isinstance(HTTPAdapter(), StateDiffable)


def test_sql_and_fs_adapters_are_state_diffable(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    assert isinstance(SQLiteAdapter(conn), StateDiffable)
    assert isinstance(FilesystemAdapter(tmp_path), StateDiffable)


def test_dry_run_state_diff_captures_added_row_and_file(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    @tool(resource="sql", reversible=True)
    def insert_widget(c: sqlite3.Connection, name: str) -> None:
        c.execute("INSERT INTO widgets (name) VALUES (?)", (name,))

    @tool(resource="fs", reversible=True)
    def write_note(handle, rel_path: str, body: bytes) -> None:
        handle.write(rel_path, body)

    adapters = {"sql": SQLiteAdapter(conn), "fs": FilesystemAdapter(tmp_path)}
    with dry_run(adapters) as ctx:
        insert_widget(name="dry-row")
        write_note(rel_path="note.txt", body=b"hello")

    diff = ctx.result.state_diff
    assert set(diff) == {"sql", "fs"}

    sql_added = diff["sql"]["rows_added"]
    assert any("dry-row" in str(row) for row in sql_added)
    assert diff["sql"]["rows_modified"] == []
    assert diff["sql"]["rows_deleted"] == []

    fs_added = diff["fs"]["files_added"]
    assert "note.txt" in fs_added
    assert diff["fs"]["files_modified"] == []
    assert diff["fs"]["files_deleted"] == []

    # Dry-run discards: the world is bit-identical afterwards.
    assert conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 0
    assert not (tmp_path / "note.txt").exists()


def test_dry_run_state_diff_detects_modify_and_delete(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Pre-existing committed state: one row, one file.
    conn.execute("INSERT INTO widgets (name) VALUES (?)", ("seed",))
    (tmp_path / "existing.txt").write_bytes(b"v1")

    @tool(resource="sql", reversible=True)
    def rename_seed(c: sqlite3.Connection) -> None:
        c.execute("UPDATE widgets SET name = ? WHERE name = ?", ("seed2", "seed"))

    @tool(resource="fs", reversible=True)
    def overwrite(handle) -> None:
        handle.write("existing.txt", b"v2")

    adapters = {"sql": SQLiteAdapter(conn), "fs": FilesystemAdapter(tmp_path)}
    with dry_run(adapters) as ctx:
        rename_seed()
        overwrite()

    diff = ctx.result.state_diff
    assert any("seed2" in str(r) for r in diff["sql"]["rows_modified"])
    assert "existing.txt" in diff["fs"]["files_modified"]


def test_dry_run_state_diff_detects_file_delete(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    (tmp_path / "existing.txt").write_bytes(b"v1")

    @tool(resource="fs", reversible=True)
    def remove(handle) -> None:
        handle.delete("existing.txt")

    adapters = {"sql": SQLiteAdapter(conn), "fs": FilesystemAdapter(tmp_path)}
    with dry_run(adapters) as ctx:
        remove()
    assert "existing.txt" in ctx.result.state_diff["fs"]["files_deleted"]
