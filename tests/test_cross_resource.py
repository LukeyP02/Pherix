"""D3 — the acceptance bar for Slice 2.

A single ``agent_txn`` carries both a SQLiteAdapter and a FilesystemAdapter;
a "fake agent" calls SQL tools and FS tools intermixed. Rollback must undo
both; commit must persist both. The runtime is unchanged beyond the D1 swap —
cross-resource routing must fall out of the adapter protocol, not from any
special-case code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.effects import EffectStatus
from pherix.core.runtime import agent_txn
from pherix.core.tools import tool
from pherix.core.transaction import TxnState


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    yield c
    c.close()


@pytest.fixture
def fs_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture
def adapters(conn, fs_root):
    return {"sql": SQLiteAdapter(conn), "fs": FilesystemAdapter(fs_root)}


@pytest.fixture
def insert_note():
    @tool(resource="sql")
    def insert_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        return body

    return insert_note


@pytest.fixture
def write_file():
    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)
        return path

    return write_file


def _note_bodies(conn):
    return [r[0] for r in conn.execute("SELECT body FROM notes ORDER BY id")]


# --- the D3 acceptance scenarios --------------------------------------------


def test_rollback_undoes_both_resources_in_one_transaction(
    conn, fs_root: Path, adapters, insert_note, write_file
):
    with agent_txn(adapters) as txn:
        insert_note(body="alpha")
        write_file(path="a.txt", data=b"alpha-payload")
        insert_note(body="beta")
        write_file(path="dir/b.txt", data=b"beta-payload")
        txn.rollback()

    # DB: empty
    assert _note_bodies(conn) == []
    # FS: no files left behind
    assert not (fs_root / "a.txt").exists()
    assert not (fs_root / "dir" / "b.txt").exists()
    assert txn.txn.state is TxnState.ROLLED_BACK


def test_commit_persists_both_resources_in_one_transaction(
    conn, fs_root: Path, adapters, insert_note, write_file
):
    with agent_txn(adapters) as txn:
        insert_note(body="alpha")
        write_file(path="a.txt", data=b"alpha-payload")
        insert_note(body="beta")
        write_file(path="dir/b.txt", data=b"beta-payload")

    assert _note_bodies(conn) == ["alpha", "beta"]
    assert (fs_root / "a.txt").read_bytes() == b"alpha-payload"
    assert (fs_root / "dir" / "b.txt").read_bytes() == b"beta-payload"
    assert txn.txn.state is TxnState.COMMITTED


def test_rollback_restores_pre_existing_file_alongside_db_rollback(
    conn, fs_root: Path, adapters, insert_note, write_file
):
    # Pre-state: one file with original contents.
    (fs_root / "doc.txt").write_bytes(b"original")

    with agent_txn(adapters) as txn:
        insert_note(body="will be rolled back")
        write_file(path="doc.txt", data=b"clobbered")
        assert (fs_root / "doc.txt").read_bytes() == b"clobbered"
        txn.rollback()

    assert _note_bodies(conn) == []
    assert (fs_root / "doc.txt").read_bytes() == b"original"


def test_exception_mid_sequence_unwinds_both_resources(
    conn, fs_root: Path, adapters, insert_note, write_file
):
    (fs_root / "keep.txt").write_bytes(b"intact")

    with pytest.raises(RuntimeError, match="agent failed"):
        with agent_txn(adapters):
            insert_note(body="x")
            write_file(path="keep.txt", data=b"corrupted")
            insert_note(body="y")
            raise RuntimeError("agent failed")

    assert _note_bodies(conn) == []
    assert (fs_root / "keep.txt").read_bytes() == b"intact"


def test_audit_records_both_adapters_in_interleaved_order(
    conn, fs_root: Path, adapters, insert_note, write_file
):
    audit = AuditJournal.in_memory()
    with agent_txn(adapters, audit=audit) as txn:
        insert_note(body="one")
        write_file(path="one.txt", data=b"1")
        insert_note(body="two")
        write_file(path="two.txt", data=b"2")

    effects = audit.get_effects(txn.txn_id)
    assert [(e["idx"], e["tool"], e["resource"], e["status"]) for e in effects] == [
        (0, "insert_note", "sql", "APPLIED"),
        (1, "write_file", "fs", "APPLIED"),
        (2, "insert_note", "sql", "APPLIED"),
        (3, "write_file", "fs", "APPLIED"),
    ]


def test_fs_backup_tempdir_is_cleaned_after_mixed_rollback(
    conn, fs_root: Path, insert_note, write_file
):
    # The FilesystemAdapter is held by name so we can inspect its tempdir
    # state before/after — and that the runtime drives commit/rollback on it
    # via the TransactionalResourceAdapter sub-protocol.
    fs_adapter = FilesystemAdapter(fs_root)
    with agent_txn({"sql": SQLiteAdapter(conn), "fs": fs_adapter}) as txn:
        assert fs_adapter.backup_root is not None
        backup_root = fs_adapter.backup_root
        insert_note(body="x")
        write_file(path="t.txt", data=b"y")
        txn.rollback()

    assert not backup_root.exists()
    assert fs_adapter.backup_root is None


def test_fs_backup_tempdir_is_cleaned_after_mixed_commit(
    conn, fs_root: Path, insert_note, write_file
):
    fs_adapter = FilesystemAdapter(fs_root)
    with agent_txn({"sql": SQLiteAdapter(conn), "fs": fs_adapter}):
        backup_root = fs_adapter.backup_root
        assert backup_root is not None
        insert_note(body="x")
        write_file(path="t.txt", data=b"y")

    assert not backup_root.exists()
    assert fs_adapter.backup_root is None
