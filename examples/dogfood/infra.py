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
from typing import Any, Iterator
from uuid import uuid4


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


@dataclass
class ScratchPG:
    """A throwaway PostgreSQL workspace — a unique schema on a real server.

    Rather than create/drop a whole database (which needs CREATEDB and a second
    connection to a maintenance DB), each scratch run gets its own uniquely-named
    *schema* on the target server, with ``search_path`` pointed at it. Teardown is
    a single ``DROP SCHEMA ... CASCADE`` — the version side-table the
    :class:`pherix.PostgresAdapter` creates lands inside this schema too, so it
    goes with it. ``conn`` is autocommit (the mode the adapter requires: it drives
    every BEGIN / SAVEPOINT / COMMIT itself). ``connect`` opens a *second*
    autocommit connection into the same schema, for the concurrent-isolation case.
    """

    dsn: str
    conn: Any
    schema: str

    def connect(self) -> Any:
        import psycopg

        c = psycopg.connect(self.dsn, autocommit=True)
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {self.schema}")
        return c


def _pg_dsn(dsn: str | None) -> str:
    """Resolve a Postgres DSN from the argument or the environment.

    The real-agent DevOps demo runs on a genuine Postgres (not SQLite — that
    reads as a toy), so the operator must point it at a server. Order: explicit
    ``dsn`` arg, then ``PHERIX_PG_DSN``, then ``DATABASE_URL``. No DSN is a loud
    failure, not a silent fallback — the demo is Postgres-only by design.
    """
    resolved = dsn or os.environ.get("PHERIX_PG_DSN") or os.environ.get("DATABASE_URL")
    if not resolved:
        raise RuntimeError(
            "scratch_postgres needs a Postgres DSN. Set PHERIX_PG_DSN (or "
            "DATABASE_URL), e.g. 'postgresql://localhost/pherix_dogfood', or pass "
            "dsn=...  The DevOps demo is Postgres-only on purpose — a real "
            "savepoint against a real server is the point. (A local server: "
            "`createdb pherix_dogfood` after installing Postgres.)"
        )
    return resolved


@contextmanager
def scratch_postgres(
    schema: str | None = None, *, dsn: str | None = None
) -> Iterator[ScratchPG]:
    """A real PostgreSQL workspace in a disposable schema, torn down on exit.

    Connects to the server named by ``dsn`` / ``PHERIX_PG_DSN`` / ``DATABASE_URL``
    in **autocommit** mode (the :class:`pherix.PostgresAdapter` contract — it owns
    every transaction boundary), creates a uniquely-named scratch schema, sets the
    ``search_path`` to it, and optionally runs ``schema`` DDL inside it. On exit
    the schema is dropped CASCADE and the connection closed. ``psycopg`` is
    imported lazily (the ``postgres`` extra), so importing this module needs no
    driver — only *calling* this does.
    """
    dsn = _pg_dsn(dsn)
    import psycopg

    schema_name = f"pherix_scratch_{uuid4().hex[:12]}"
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema_name}")
            cur.execute(f"SET search_path TO {schema_name}")
        if schema:
            # psycopg's extended protocol is one-statement-per-execute; the
            # seed DDL is a small script, so split on ';' and run each part.
            for stmt in (s.strip() for s in schema.split(";")):
                if stmt:
                    conn.execute(stmt)
        yield ScratchPG(dsn=dsn, conn=conn, schema=schema_name)
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
        finally:
            conn.close()


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
