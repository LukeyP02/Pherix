"""Slice 2 dogfood — a mixed-resource fake agent against SQL + filesystem.

Run:  python examples/slice2_demo.py

Shows D3 end-to-end: one ``agent_txn`` carries both adapters, the "agent"
interleaves DB writes and file writes, and a single mid-sequence rollback
wipes *both* resources — no special-case routing in the runtime. The audit
journal records every effect (across both adapters) in the order they ran.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the example runnable as `python examples/slice2_demo.py` without an
# editable install — put the repo root on the path before importing pherix.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (
    AuditJournal,
    FilesystemAdapter,
    SQLiteAdapter,
    agent_txn,
    tool,
)
from pherix.core.adapters.filesystem import FsHandle


@tool(resource="sql")
def index_doc(conn, doc_id: str, title: str):
    conn.execute(
        "INSERT INTO docs (id, title) VALUES (?, ?)", (doc_id, title)
    )
    return doc_id


@tool(resource="fs")
def store_doc(fs: FsHandle, doc_id: str, body: bytes):
    fs.write(f"{doc_id}.txt", body)
    return doc_id


def fake_agent_publish(batch):
    """Plain agent step: for each doc, write index row + payload file."""
    for doc_id, title, body in batch:
        index_doc(doc_id=doc_id, title=title)
        store_doc(doc_id=doc_id, body=body)


def main() -> None:
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    fs_root = Path(tempfile.mkdtemp(prefix="pherix_demo_"))

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")

    audit = AuditJournal()
    adapters = {
        "sql": SQLiteAdapter(conn),
        "fs": FilesystemAdapter(fs_root),
    }

    def show(label: str) -> None:
        rows = conn.execute("SELECT id, title FROM docs ORDER BY id").fetchall()
        files = sorted(p.name for p in fs_root.iterdir())
        print(f"  {label}")
        print(f"    DB   : {rows}")
        print(f"    FILES: {files}")

    print("Transaction 1 — publish a batch, then notice a bad title and roll back")
    with agent_txn(adapters, audit=audit) as txn:
        fake_agent_publish(
            [
                ("doc-1", "Intro", b"# Intro\nhello"),
                ("doc-2", "TODO change me", b"# TODO\nplaceholder"),
            ]
        )
        show("mid-transaction (both resources live)")
        print("  ! placeholder title escaped — rolling back the whole batch")
        txn.rollback()
    t1 = txn.txn_id
    show("after rollback — both resources empty")
    print()

    print("Transaction 2 — agent retries cleanly and commits")
    with agent_txn(adapters, audit=audit) as txn:
        fake_agent_publish(
            [
                ("doc-1", "Intro", b"# Intro\nhello"),
                ("doc-2", "Methods", b"# Methods\n..."),
            ]
        )
        show("mid-transaction")
    t2 = txn.txn_id
    show("after commit — both resources persisted")
    print()

    print("Audit journal — the whole story, across both adapters:")
    for tid in (t1, t2):
        record = audit.get_transaction(tid)
        print(f"  {tid}  state={record['state']}")
        for effect in audit.get_effects(tid):
            print(
                f"    [{effect['idx']}] {effect['resource']:>3}  "
                f"{effect['tool']}({effect['args']}) -> {effect['status']}"
            )

    conn.close()
    os.remove(db_path)
    shutil.rmtree(fs_root)


if __name__ == "__main__":
    main()
