"""Slice 4 — VersionedResourceAdapter conformance and behaviour.

Tests for the read/write version tag space underpinning the commit-time
isolation diff. SQL versions are monotonic counters held in the
``_pherix_versions`` side-table; FS versions are sha256 content hashes
(or the literal ``"__missing__"`` for absent paths). HTTPAdapter is
explicitly *non-conforming*: its read/write_version raise loudly because
irreversible effects are isolated-by-construction via staging.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter
from pherix.core.adapters.http import HTTPAdapter, IrreversibleAdapterError
from pherix.core.adapters.sql import SQLiteAdapter


# --- SQLiteAdapter -----------------------------------------------------------


@pytest.fixture
def sql_conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    yield c
    c.close()


def test_sql_side_table_is_created_on_init(sql_conn):
    SQLiteAdapter(sql_conn)
    rows = sql_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = '_pherix_versions'"
    ).fetchall()
    assert rows == [("_pherix_versions",)]


def test_sql_read_version_returns_zero_for_unknown_key(sql_conn):
    a = SQLiteAdapter(sql_conn)
    assert a.read_version(("counters", "x")) == 0
    # Never None — that contract matters for the commit-time diff.
    assert a.read_version(("counters", "x")) is not None


def test_sql_write_version_bumps_and_returns_new_version(sql_conn):
    a = SQLiteAdapter(sql_conn)
    key = ("counters", "x")
    assert a.write_version(key) == 1
    assert a.write_version(key) == 2
    assert a.write_version(key) == 3
    # read_version sees the same value the last write returned
    assert a.read_version(key) == 3


def test_sql_versions_are_per_key_independent(sql_conn):
    a = SQLiteAdapter(sql_conn)
    a.write_version(("counters", "x"))
    a.write_version(("counters", "x"))
    a.write_version(("counters", "y"))
    assert a.read_version(("counters", "x")) == 2
    assert a.read_version(("counters", "y")) == 1
    assert a.read_version(("counters", "z")) == 0


def test_sql_two_adapters_on_same_file_see_each_others_bumps(tmp_path: Path):
    # Multi-process arbitration story: two SQLiteAdapter instances pointing
    # at the same on-disk SQLite file (simulating two Python processes)
    # must agree on the version counter via the shared side-table.
    db_path = tmp_path / "shared.db"
    conn_a = sqlite3.connect(str(db_path), isolation_level=None)
    conn_b = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        a = SQLiteAdapter(conn_a)
        b = SQLiteAdapter(conn_b)
        key = ("counters", "x")

        assert a.write_version(key) == 1
        # B (a different connection) sees A's bump
        assert b.read_version(key) == 1

        assert b.write_version(key) == 2
        assert a.read_version(key) == 2
    finally:
        conn_a.close()
        conn_b.close()


def test_sql_key_encoding_is_stable_for_int_and_str_pks(sql_conn):
    a = SQLiteAdapter(sql_conn)
    # Distinct key shapes must map to distinct rows.
    a.write_version(("users", 1))
    a.write_version(("users", "1"))
    assert a.read_version(("users", 1)) == 1
    assert a.read_version(("users", "1")) == 1
    # And bumping one does not bump the other.
    a.write_version(("users", 1))
    assert a.read_version(("users", 1)) == 2
    assert a.read_version(("users", "1")) == 1


# --- FilesystemAdapter -------------------------------------------------------


@pytest.fixture
def fs_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


def test_fs_read_version_returns_missing_sentinel_for_nonexistent(fs_root: Path):
    a = FilesystemAdapter(fs_root)
    assert a.read_version(("ghost.txt",)) == "__missing__"


def test_fs_read_version_returns_content_hash(fs_root: Path):
    (fs_root / "file.txt").write_bytes(b"hello world")
    a = FilesystemAdapter(fs_root)
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert a.read_version(("file.txt",)) == expected


def test_fs_identical_contents_hash_identically(fs_root: Path, tmp_path: Path):
    other_root = tmp_path / "other"
    other_root.mkdir()
    (fs_root / "a.txt").write_bytes(b"same bytes")
    (other_root / "b.txt").write_bytes(b"same bytes")
    a1 = FilesystemAdapter(fs_root)
    a2 = FilesystemAdapter(other_root)
    assert a1.read_version(("a.txt",)) == a2.read_version(("b.txt",))


def test_fs_different_contents_hash_differently(fs_root: Path):
    (fs_root / "a.txt").write_bytes(b"alpha")
    (fs_root / "b.txt").write_bytes(b"beta")
    a = FilesystemAdapter(fs_root)
    assert a.read_version(("a.txt",)) != a.read_version(("b.txt",))


def test_fs_write_version_returns_on_disk_hash(fs_root: Path):
    (fs_root / "f.txt").write_bytes(b"v1")
    a = FilesystemAdapter(fs_root)
    expected = hashlib.sha256(b"v1").hexdigest()
    assert a.write_version(("f.txt",)) == expected
    # Mutate the file externally and re-call: write_version reads the
    # *current* on-disk content (no cache).
    (fs_root / "f.txt").write_bytes(b"v2")
    expected2 = hashlib.sha256(b"v2").hexdigest()
    assert a.write_version(("f.txt",)) == expected2


def test_fs_missing_sentinel_differs_from_any_hash(fs_root: Path):
    # The "absent" case must be distinguishable from any real hash via !=.
    # That's the whole point of choosing "__missing__" over None.
    a = FilesystemAdapter(fs_root)
    absent = a.read_version(("ghost.txt",))
    (fs_root / "ghost.txt").write_bytes(b"")  # empty file → real sha256
    present = a.read_version(("ghost.txt",))
    assert absent != present
    assert absent == "__missing__"


def test_fs_version_key_rejects_path_traversal(fs_root: Path):
    a = FilesystemAdapter(fs_root)
    with pytest.raises(ValueError, match="outside root"):
        a.read_version(("../escape.txt",))


def test_fs_version_key_must_be_one_tuple(fs_root: Path):
    a = FilesystemAdapter(fs_root)
    with pytest.raises(ValueError, match="1-tuple"):
        a.read_version(("a", "b"))


# --- HTTPAdapter (non-conforming) -------------------------------------------


def test_http_read_version_raises_irreversible_adapter_error():
    with pytest.raises(IrreversibleAdapterError, match="read_version"):
        HTTPAdapter().read_version(("anything",))


def test_http_write_version_raises_irreversible_adapter_error():
    with pytest.raises(IrreversibleAdapterError, match="write_version"):
        HTTPAdapter().write_version(("anything",))


def test_http_supports_rollback_remains_the_isolation_gate():
    # Stream C's contract: isolation work is gated on supports_rollback().
    # HTTPAdapter must still report False so the runtime exempts it from
    # versioning checks — that's the honest non-conformance signal.
    assert HTTPAdapter().supports_rollback() is False


# --- library export ---------------------------------------------------------


def test_versioned_resource_adapter_is_exported_from_library():
    from pherix.frontends import library

    assert hasattr(library, "VersionedResourceAdapter")
    assert "VersionedResourceAdapter" in library.__all__
