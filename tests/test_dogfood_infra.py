"""Offline checks for the disposable-infra helpers the dogfoods stand on."""

import shutil
from pathlib import Path

import pytest

from examples.dogfood.infra import scratch_repo, scratch_sqlite, temp_tree


def test_scratch_sqlite_is_real_on_disk_and_shareable():
    with scratch_sqlite("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)") as db:
        assert Path(db.path).exists()
        db.conn.execute("INSERT INTO t (v) VALUES ('a')")
        # A second connection to the same file sees the committed row — this is
        # what the concurrent / cross-process isolation demos rely on.
        other = db.connect()
        try:
            rows = other.execute("SELECT v FROM t").fetchall()
            assert rows == [("a",)]
        finally:
            other.close()
    # File cleaned up on exit.
    assert not Path(db.path).exists()


def test_temp_tree_seeds_and_cleans_up():
    with temp_tree({"a.txt": "hello", "sub/b.bin": b"\x00\x01"}) as root:
        assert (root / "a.txt").read_text() == "hello"
        assert (root / "sub/b.bin").read_bytes() == b"\x00\x01"
    assert not root.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_scratch_repo_starts_from_a_clean_initial_commit():
    import subprocess

    with scratch_repo({"README.md": "# scratch\n"}) as root:
        assert (root / "README.md").exists()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        # Clean tree: the seed file was committed in the initial commit.
        assert status.stdout.strip() == ""
    assert not root.exists()
