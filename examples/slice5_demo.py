"""Slice 5 dogfood — replay the journal forward against fresh state.

Run:  python examples/slice5_demo.py

The demo opens a source transaction that interleaves SQL inserts, FS
writes, and an irreversible HTTP fire — the same shape as Slice 2's mixed
journal — and then replays it three times against truly-fresh adapters.

  1. Verify mode against fresh state — every effect's recorded result
     equals the replayed result under the default JSON comparator.
  2. Reconstruct mode on yet-another fresh substrate — the world is
     rebuilt from the journal; the SQL row is there, the file's bytes
     match, the irreversible HTTP fire is reused from the journal (NOT
     re-called, so the external world isn't double-billed).
  3. Tamper the source audit row, replay verify → divergence. Pherix
     reports which effect mismatched and which result it expected.

Maths framing: commit and rollback were a forward fold and a backward
fold of the journal. Replay is a forward fold of the journal against a
*different* starting state. ``mode='verify'`` asserts the fold lands on
the same observables; ``mode='reconstruct'`` accepts the new observables
as the rebuilt world.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the example runnable as `python examples/slice5_demo.py` without an
# editable install — put the repo root on the path before importing pherix.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (
    AuditJournal,
    FilesystemAdapter,
    HTTPAdapter,
    ReplayDivergence,
    SQLiteAdapter,
    agent_txn,
    replay,
    tool,
)
from pherix.core.adapters.filesystem import FsHandle


# --- tools ------------------------------------------------------------------


@tool(resource="sql")
def index_doc(conn, doc_id: str, title: str):
    conn.execute("INSERT INTO docs (id, title) VALUES (?, ?)", (doc_id, title))
    return doc_id


@tool(resource="fs")
def store_doc(fs: FsHandle, doc_id: str, body: bytes):
    fs.write(f"{doc_id}.txt", body)
    return doc_id


# A counter so we can show that replay does NOT re-fire APPLIED irreversibles.
NOTIFY_FIRES: list[str] = []


@tool(resource="http", reversible=False, injects_handle=False, name="notify")
def notify(doc_id: str):
    NOTIFY_FIRES.append(doc_id)
    return {"status": "ok", "doc_id": doc_id}


# --- helpers ---------------------------------------------------------------


def _fresh_sql_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    return conn


def _show(label: str, conn: sqlite3.Connection, fs_root: Path) -> None:
    rows = conn.execute("SELECT id, title FROM docs ORDER BY id").fetchall()
    files = sorted(p.name for p in fs_root.iterdir())
    print(f"    {label}")
    print(f"      DB   : {rows}")
    print(f"      FILES: {files}")


def _show_outcomes(result) -> None:
    for o in result.outcomes:
        if o.message:
            print(f"      [{o.index}] {o.tool:>12}  {o.status:<22}  {o.message}")
        else:
            print(
                f"      [{o.index}] {o.tool:>12}  {o.status:<22}"
                f"  recorded={o.recorded_result!r} replayed={o.replayed_result!r}"
            )


# --- demo body --------------------------------------------------------------


def main() -> None:
    audit_fd, audit_path = tempfile.mkstemp(suffix=".db")
    os.close(audit_fd)
    source_audit = AuditJournal(audit_path)

    # --- source transaction ------------------------------------------------
    src_conn = _fresh_sql_db()
    src_fs_root = Path(tempfile.mkdtemp(prefix="pherix_src_"))
    src_adapters = {
        "sql": SQLiteAdapter(src_conn),
        "fs": FilesystemAdapter(src_fs_root),
        "http": HTTPAdapter(),
    }

    print("Source transaction — publish a doc across SQL + FS + HTTP")
    with agent_txn(src_adapters, audit=source_audit) as ctx:
        index_doc(doc_id="d1", title="Intro")
        store_doc(doc_id="d1", body=b"# Intro\nhello slice 5")
        r = notify(doc_id="d1")
        ctx.approve_irreversible(r.effect_id)
    src_txn_id = ctx.txn_id
    _show("source state after commit", src_conn, src_fs_root)
    print(f"      NOTIFY_FIRES (irreversible call count): {NOTIFY_FIRES}")
    print()

    # --- (1) verify against fresh state ------------------------------------
    fresh_conn = _fresh_sql_db()
    fresh_fs_root = Path(tempfile.mkdtemp(prefix="pherix_verify_"))
    print("(1) Verify mode — fresh DB + fresh FS, journal re-fired & compared")
    result = replay(
        src_txn_id,
        {
            "sql": SQLiteAdapter(fresh_conn),
            "fs": FilesystemAdapter(fresh_fs_root),
            "http": HTTPAdapter(),
        },
        source_audit=source_audit,
        mode="verify",
    )
    print(f"    status={result.status!r}  replay_txn_id={result.replay_txn_id}")
    _show_outcomes(result)
    _show("verify outcome state", fresh_conn, fresh_fs_root)
    print(f"      NOTIFY_FIRES still: {NOTIFY_FIRES}   (HTTP not re-fired)")
    print()

    # --- (2) reconstruct -----------------------------------------------------
    fresh2_conn = _fresh_sql_db()
    fresh2_fs_root = Path(tempfile.mkdtemp(prefix="pherix_recon_"))
    print("(2) Reconstruct mode — rebuild the world from the journal")
    result2 = replay(
        src_txn_id,
        {
            "sql": SQLiteAdapter(fresh2_conn),
            "fs": FilesystemAdapter(fresh2_fs_root),
            "http": HTTPAdapter(),
        },
        source_audit=source_audit,
        mode="reconstruct",
    )
    print(f"    status={result2.status!r}")
    _show("reconstruct outcome state", fresh2_conn, fresh2_fs_root)
    print(f"      NOTIFY_FIRES still: {NOTIFY_FIRES}   (HTTP not re-fired)")
    print()

    # --- (3) tamper, replay, watch verify diverge --------------------------
    source_audit.close()
    raw = sqlite3.connect(audit_path)
    raw.execute(
        "UPDATE effects SET result = ? WHERE txn_id = ? AND tool = 'index_doc'",
        (json.dumps("MALLORY"), src_txn_id),
    )
    raw.commit()
    raw.close()
    source_audit = AuditJournal(audit_path)

    fresh3_conn = _fresh_sql_db()
    fresh3_fs_root = Path(tempfile.mkdtemp(prefix="pherix_tamper_"))
    print("(3) Tampered audit row — verify catches it")
    try:
        replay(
            src_txn_id,
            {
                "sql": SQLiteAdapter(fresh3_conn),
                "fs": FilesystemAdapter(fresh3_fs_root),
                "http": HTTPAdapter(),
            },
            source_audit=source_audit,
            mode="verify",
        )
    except ReplayDivergence as exc:
        print(f"    ReplayDivergence: {exc}")
        for d in exc.result.divergences:
            print(
                f"      [{d.index}] {d.tool}  recorded={d.recorded_result!r}  "
                f"replayed={d.replayed_result!r}"
            )

    # --- cleanup -----------------------------------------------------------
    source_audit.close()
    os.remove(audit_path)
    shutil.rmtree(src_fs_root)
    shutil.rmtree(fresh_fs_root)
    shutil.rmtree(fresh2_fs_root)
    shutil.rmtree(fresh3_fs_root)


if __name__ == "__main__":
    main()
