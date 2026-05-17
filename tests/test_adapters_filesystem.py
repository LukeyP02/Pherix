"""Unit tests for FilesystemAdapter + FsHandle (Slice 2).

These exercise the adapter directly with synthesized Effects. The
cross-resource integration with agent_txn / SQLiteAdapter lives in
``test_cross_resource.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pherix.core.adapters.base import (
    ResourceAdapter,
    TransactionalResourceAdapter,
)
from pherix.core.adapters.filesystem import FilesystemAdapter, FsHandle
from pherix.core.effects import Effect, EffectStatus


def _effect(index: int = 0) -> Effect:
    return Effect(
        txn_id="t",
        index=index,
        tool="fake",
        args={},
        resource="fs",
        reversible=True,
    )


def _snap(adapter: "FilesystemAdapter", effect: Effect):
    """Mirror what the runtime does: snapshot, store on effect.snapshot."""
    effect.snapshot = adapter.snapshot(effect)
    return effect.snapshot


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "root"


@pytest.fixture
def adapter(root: Path) -> FilesystemAdapter:
    root.mkdir()
    a = FilesystemAdapter(root)
    a.begin()
    yield a
    # If a test didn't finalise the adapter, still tear down.
    try:
        a.rollback()
    except Exception:
        pass


# --- protocol conformance ----------------------------------------------------


def test_filesystem_adapter_satisfies_resource_adapter_protocol(root: Path):
    root.mkdir()
    a = FilesystemAdapter(root)
    assert isinstance(a, ResourceAdapter)


def test_filesystem_adapter_satisfies_transactional_sub_protocol(root: Path):
    root.mkdir()
    a = FilesystemAdapter(root)
    assert isinstance(a, TransactionalResourceAdapter)


def test_supports_rollback_is_true(root: Path):
    root.mkdir()
    assert FilesystemAdapter(root).supports_rollback() is True


# --- snapshot / apply / restore round-trip -----------------------------------


def test_modified_file_restores_to_original_contents(adapter: FilesystemAdapter, root: Path):
    target = root / "doc.txt"
    target.write_bytes(b"original")

    effect = _effect()
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("doc.txt", b"modified")

    adapter.apply(effect, tool)
    assert target.read_bytes() == b"modified"

    adapter.restore(handle)
    assert target.read_bytes() == b"original"


def test_new_file_is_deleted_on_restore_no_orphan(adapter: FilesystemAdapter, root: Path):
    effect = _effect()
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("new.txt", b"hello")

    adapter.apply(effect, tool)
    assert (root / "new.txt").exists()

    adapter.restore(handle)
    assert not (root / "new.txt").exists()


def test_deleted_pre_existing_file_is_recreated_on_restore(adapter: FilesystemAdapter, root: Path):
    target = root / "keep.txt"
    target.write_bytes(b"precious")

    effect = _effect()
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.delete("keep.txt")

    adapter.apply(effect, tool)
    assert not target.exists()

    adapter.restore(handle)
    assert target.read_bytes() == b"precious"


# --- lazy snapshot rule ------------------------------------------------------


def test_multiple_writes_same_path_in_one_effect_back_up_once(
    adapter: FilesystemAdapter, root: Path
):
    target = root / "log.txt"
    target.write_bytes(b"v0")

    effect = _effect()
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("log.txt", b"v1")
        fs.write("log.txt", b"v2")
        fs.write("log.txt", b"v3")

    adapter.apply(effect, tool)
    # Restore must land on the pre-effect state — v0, not the intermediate v1.
    adapter.restore(handle)
    assert target.read_bytes() == b"v0"


def test_reads_do_not_trigger_backups(adapter: FilesystemAdapter, root: Path):
    (root / "ref.txt").write_bytes(b"data")

    effect = _effect()
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        assert fs.read("ref.txt") == b"data"

    adapter.apply(effect, tool)
    # touched payload must be empty — read alone backs up nothing
    assert handle.payload["touched"] == {}


# --- path safety -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "../etc/passwd",
        "subdir/../../escape.txt",
        "/etc/passwd",
        "/tmp/absolute.txt",
    ],
)
def test_write_rejects_path_traversal(adapter: FilesystemAdapter, bad_path: str):
    effect = _effect()
    _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write(bad_path, b"x")

    with pytest.raises(ValueError, match="outside root"):
        adapter.apply(effect, tool)


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.txt", "/etc/hosts"],
)
def test_read_rejects_path_traversal(adapter: FilesystemAdapter, bad_path: str):
    effect = _effect()
    _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.read(bad_path)

    with pytest.raises(ValueError, match="outside root"):
        adapter.apply(effect, tool)


def test_delete_rejects_path_traversal(adapter: FilesystemAdapter):
    effect = _effect()
    _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.delete("../something.txt")

    with pytest.raises(ValueError, match="outside root"):
        adapter.apply(effect, tool)


def test_symlink_escaping_root_is_rejected(adapter: FilesystemAdapter, root: Path, tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"shh")
    (root / "escape_link").symlink_to(secret)

    effect = _effect()
    _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("escape_link", b"pwned")

    with pytest.raises(ValueError, match="outside root"):
        adapter.apply(effect, tool)


# --- backup tempdir lifecycle ------------------------------------------------


def test_begin_creates_tempdir_commit_removes_it(root: Path):
    root.mkdir()
    a = FilesystemAdapter(root)
    a.begin()
    backup_root = a.backup_root
    assert backup_root is not None
    assert backup_root.exists()
    a.commit()
    assert not backup_root.exists()
    assert a.backup_root is None


def test_begin_creates_tempdir_rollback_removes_it(root: Path):
    root.mkdir()
    a = FilesystemAdapter(root)
    a.begin()
    backup_root = a.backup_root
    assert backup_root.exists()
    a.rollback()
    assert not backup_root.exists()
    assert a.backup_root is None


def test_per_effect_subdir_lives_under_backup_root(adapter: FilesystemAdapter, root: Path):
    (root / "f.txt").write_bytes(b"x")
    effect = _effect(index=7)
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("f.txt", b"y")

    adapter.apply(effect, tool)
    backup_dir = Path(handle.payload["backup_dir"])
    assert backup_dir.exists()
    # subdir is under the per-txn backup root
    assert backup_dir.parent == adapter.backup_root
    # and contains the actual backup file referenced by payload
    backup_name = handle.payload["touched"]["f.txt"]["backup"]
    assert backup_name is not None
    assert (backup_dir / backup_name).read_bytes() == b"x"


def test_payload_is_json_serialisable(adapter: FilesystemAdapter, root: Path):
    import json

    (root / "a.txt").write_bytes(b"a")
    effect = _effect()
    handle = _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("a.txt", b"b")
        fs.write("new.txt", b"c")

    adapter.apply(effect, tool)
    # The audit journal serialises payloads with json.dumps; this must not throw.
    json.dumps(handle.payload)


# --- write semantics ---------------------------------------------------------


def test_write_creates_parent_directories(adapter: FilesystemAdapter, root: Path):
    effect = _effect()
    _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.write("a/b/c/leaf.txt", b"hi")

    adapter.apply(effect, tool)
    assert (root / "a" / "b" / "c" / "leaf.txt").read_bytes() == b"hi"


def test_delete_of_missing_file_raises(adapter: FilesystemAdapter):
    effect = _effect()
    _snap(adapter, effect)

    def tool(fs: FsHandle):
        fs.delete("ghost.txt")

    with pytest.raises(FileNotFoundError):
        adapter.apply(effect, tool)


def test_two_effects_same_path_backward_fold_lands_at_original(
    adapter: FilesystemAdapter, root: Path
):
    # Pre-state: file at "v0". Effect 0 writes "v1"; effect 1 writes "v2".
    # Restoring newest-first (e1 then e0) must land back at "v0".
    target = root / "shared.txt"
    target.write_bytes(b"v0")

    e0 = _effect(index=0)
    h0 = _snap(adapter, e0)

    def tool0(fs: FsHandle):
        fs.write("shared.txt", b"v1")

    adapter.apply(e0, tool0)
    assert target.read_bytes() == b"v1"

    e1 = _effect(index=1)
    h1 = _snap(adapter, e1)

    def tool1(fs: FsHandle):
        fs.write("shared.txt", b"v2")

    adapter.apply(e1, tool1)
    assert target.read_bytes() == b"v2"

    # Backward fold: newest first
    adapter.restore(h1)
    assert target.read_bytes() == b"v1"
    adapter.restore(h0)
    assert target.read_bytes() == b"v0"
