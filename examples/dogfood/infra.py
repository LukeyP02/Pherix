"""Disposable-infra helpers for the dogfoods — real, but throwaway.

Each context manager stands up genuine infrastructure (a real on-disk SQLite
file, a real git repo, a real directory tree) and tears it down on exit. The
dogfoods need *real* backends so Pherix's adapters do real work — a savepoint
against an in-memory stub proves nothing about the on-disk path that two
concurrent agents share.

Offline and key-free: nothing here talks to a network or reads a secret. Safe
to exercise from the pytest suite.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class ScratchDB:
    """A throwaway on-disk SQLite database.

    ``conn`` is the primary connection (autocommit, the mode
    :class:`pherix.SQLiteAdapter` requires). ``path`` is the file on disk —
    exposed so a test or dogfood can open a *second* connection to the same
    file via :meth:`connect`, which is exactly what cross-process / concurrent
    isolation demos need. Connections opened via :meth:`connect` are the
    caller's to close; the primary ``conn`` and the file are cleaned up when
    the :func:`scratch_sqlite` block exits.
    """

    path: str
    conn: sqlite3.Connection

    def connect(self) -> sqlite3.Connection:
        """Open another autocommit connection to the same on-disk file."""
        return sqlite3.connect(self.path, isolation_level=None)


@contextmanager
def scratch_sqlite(schema: str | None = None) -> Iterator[ScratchDB]:
    """A real on-disk SQLite DB, optionally pre-seeded with ``schema``.

    On-disk (not ``:memory:``) so multiple connections can share it — the
    audit dogfood runs two concurrent agents against one file. Autocommit
    (``isolation_level=None``) so the Pherix adapter owns every
    BEGIN/SAVEPOINT/COMMIT.
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pherix_scratch_")
    os.close(fd)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        if schema:
            conn.executescript(schema)
        yield ScratchDB(path=path, conn=conn)
    finally:
        conn.close()
        # WAL/SHM siblings may exist; remove the lot, ignore if absent.
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


@contextmanager
def temp_tree(files: dict[str, str | bytes] | None = None) -> Iterator[Path]:
    """A throwaway directory tree, optionally seeded with ``{relpath: content}``.

    The natural root for a :class:`pherix.FilesystemAdapter`. ``content`` may be
    ``str`` (written as UTF-8) or ``bytes``.
    """
    root = Path(tempfile.mkdtemp(prefix="pherix_tree_"))
    try:
        for rel, content in (files or {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def scratch_repo(files: dict[str, str] | None = None) -> Iterator[Path]:
    """A throwaway git repo with one initial commit.

    Used by the coding sandbox (a real CoW overlay needs a real repo) and any
    dogfood that wants version-controlled scratch state. Requires ``git`` on
    PATH — a dev-machine assumption; the failure is loud if it is missing.
    """
    root = Path(tempfile.mkdtemp(prefix="pherix_repo_"))
    try:
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "dogfood@pherix.dev")
        _git(root, "config", "user.name", "Pherix Dogfood")
        for rel, content in (files or {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "initial")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _git(cwd: Path, *args: str) -> None:
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "scratch_repo needs `git` on PATH; it was not found."
        ) from exc
