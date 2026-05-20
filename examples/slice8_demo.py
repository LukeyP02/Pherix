"""Slice 8 dogfood — an MCP client driving the same Pherix engine.

Run:  python examples/slice8_demo.py

The gateway is a *second driver*, not a second engine. A mock MCP client
speaks JSON-RPC 2.0 to a :class:`PherixGateway` in-process (no subprocess),
handshakes an identity, discovers the operator's ``@tool`` functions, and
calls them — each call wrapped in a Pherix transaction by the core. The
agent needs zero Pherix code; it just speaks MCP.

Three scenarios:

  1. Handshake + discovery. The client initialises with an identity and
     lists the tools the gateway exposes.
  2. A committed transaction through the gateway. One tool call lands as
     a real ``agent_txn`` — the journal materialises, the row persists,
     and the audit transaction is attributed to the handshake identity
     via the ``client_id`` column.
  3. A dry-run through the gateway. The same call routed speculatively:
     the ``state_diff`` shows the rows / files a real commit *would* have
     touched, and the world is byte-identical afterwards.

Maths framing: ``agent_txn`` and the gateway are two maps into the same
journal monoid. The gateway adds a coordinate — the handshake identity,
recorded as ``client_id`` — but the fold itself (snapshot → apply →
journal → commit/rollback) is the engine's, untouched. Same algebra, a
second observer.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the example runnable as `python examples/slice8_demo.py` without an
# editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (
    AuditJournal,
    FilesystemAdapter,
    InProcessMCPClient,
    PherixGateway,
    Policy,
    SQLiteAdapter,
    tool,
)
from pherix.core.adapters.filesystem import FsHandle
from pherix.core.tools import REGISTRY


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _note_rows(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT id, body FROM notes ORDER BY id")]


def main() -> None:
    REGISTRY.clear()

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    fs_root = Path(tempfile.mkdtemp(prefix="pherix-slice8-"))
    audit = AuditJournal.in_memory()

    @tool(resource="sql")
    def add_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        return {"added": body}

    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)

    gateway = PherixGateway(
        adapters={
            "sql": SQLiteAdapter(conn),
            "fs": FilesystemAdapter(fs_root),
        },
        default_policy=Policy.allow_all(),
        audit=audit,
    )

    # --- 1. handshake + discovery -------------------------------------------
    _banner("1. MCP handshake + tool discovery")
    client = InProcessMCPClient(gateway)
    init = InProcessMCPClient.result_of(client.initialize("claude-code"))
    print(f"  initialize ->     server={init.get('serverInfo')}")
    tools = client.tool_descriptors()
    print(f"  tools/list ->     {[t['name'] for t in tools]}")

    # --- 2. committed transaction through the gateway -----------------------
    _banner("2. Committed transaction through the gateway")
    resp = client.call_tool("add_note", {"body": "from the gateway"})
    result = InProcessMCPClient.structured_of(resp)
    print(f"  tools/call result: {result}")
    print(f"  SQL rows:          {_note_rows(conn)}")

    # The audit transaction is attributed to the handshake identity via the
    # txn_id the gateway returned — the contract surface, not a table peek.
    txn_row = audit.get_transaction(result["txn_id"])
    print(f"  audit txn state:   {txn_row['state']}")
    print(f"  client_id:         {txn_row.get('client_id')!r}  (handshake identity)")
    print("  journal:")
    for e in audit.get_effects(result["txn_id"]):
        print(f"      [{e['idx']}] {e['tool']}({e['args']}) "
              f"resource={e['resource']} status={e['status']}")

    # --- 3. dry-run through the gateway -------------------------------------
    _banner("3. Dry-run through the gateway (state-diff, world untouched)")
    pre_rows = _note_rows(conn)
    dry = client.call_tool(
        "write_file", {"path": "draft.txt", "data": b"would-be-content"},
        dry_run=True,
    )
    dry_result = InProcessMCPClient.structured_of(dry)
    diff = dry_result["dry_run_result"]["state_diff"]
    print(f"  is_clean:          {dry_result['dry_run_result']['is_clean']}")
    print(f"  state_diff (sql):  {diff['sql']}")
    print(f"  state_diff (fs):   {diff['fs']}")
    print(f"  SQL rows before:   {pre_rows}")
    print(f"  SQL rows after:    {_note_rows(conn)}  (unchanged)")
    print(f"  draft.txt exists?  {(fs_root / 'draft.txt').exists()}  (never written)")

    conn.close()
    print()
    print("Slice 8 demo done.")


if __name__ == "__main__":
    main()
