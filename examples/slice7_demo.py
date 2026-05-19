"""Slice 7 dogfood — agent_txn that rolls back at the end.

Run:  python examples/slice7_demo.py

Three scenarios, each one a different way the journal records what
*would* have happened without anything actually happening:

  1. Clean dry-run on a mixed SQL+FS+HTTP txn. The journal materialises
     in full; the would_have_fired list surfaces the staged irreversibles
     by themselves; every policy verdict is Allow; SQL state, file tree,
     and HTTP-call counter are byte-identical before and after.

  2. Policy denial captured. A rule that would normally raise
     mid-body instead lands as a Deny verdict in the result. The agent
     body keeps running, the full journal materialises, and the
     operator sees exactly which rule fired against which effect.

  3. World-state assertion. Two snapshots — SQL rows + file-tree hashes
     — taken before and after a substantial dry-run, asserted equal.
     The load-bearing invariant the slice ships.

Maths framing: a real transaction is a forward fold of the journal
ending in ``adapter.commit``. A dry-run is the same forward fold ending
in ``adapter.rollback`` — the *measurement without collapse*. What you
observe is the journal-as-built (plus the policy verdicts that would
have fired); what survives is nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the example runnable as `python examples/slice7_demo.py` without an
# editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pherix import (
    Allow,
    Deny,
    FilesystemAdapter,
    HTTPAdapter,
    Policy,
    SQLiteAdapter,
    dry_run,
    tool,
)
from pherix.core.adapters.filesystem import FsHandle
from pherix.core.tools import REGISTRY


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _fs_snapshot(root: Path) -> dict[str, str]:
    """Content-addressed hash of every file under ``root``."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
    return out


def scenario_1_clean_mixed_txn() -> None:
    _banner("1. Clean dry-run on a mixed SQL+FS+HTTP transaction")
    REGISTRY.clear()

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT)")
    tmpdir = Path(tempfile.mkdtemp(prefix="pherix-dryrun-"))
    notify_fires: list[str] = []

    @tool(resource="sql")
    def add_doc(conn, title):
        conn.execute("INSERT INTO docs (title) VALUES (?)", (title,))

    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)

    @tool(resource="http", reversible=False, injects_handle=False)
    def notify(doc_id):
        notify_fires.append(doc_id)

    with dry_run(
        {
            "sql": SQLiteAdapter(conn),
            "fs": FilesystemAdapter(tmpdir),
            "http": HTTPAdapter(),
        }
    ) as ctx:
        add_doc(title="Intro")
        write_file(path="intro.md", data=b"# Intro\n")
        notify(doc_id="d1")

    result = ctx.result
    print(f"  journal:           {len(result.journal)} effects")
    for e in result.journal:
        print(f"      [{e.index}] {e.tool}({e.args}) resource={e.resource} "
              f"status={e.status.name}")
    print(f"  would_have_fired:  {[e.tool for e in result.would_have_fired]}")
    print(f"  policy_verdicts:   {len(result.policy_verdicts)} "
          f"(all_allow={result.is_clean})")
    print(f"  SQL rows:          {list(conn.execute('SELECT * FROM docs'))}")
    print(f"  FS files:          {_fs_snapshot(tmpdir)}")
    print(f"  notify_fires:      {notify_fires}")
    conn.close()


def scenario_2_policy_denial_captured() -> None:
    _banner("2. Policy denial captured (body keeps running)")
    REGISTRY.clear()

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, tier TEXT)")

    @tool(resource="sql")
    def update_user(conn, user_id, tier):
        conn.execute(
            "INSERT INTO users (id, tier) VALUES (?, ?)", (user_id, tier)
        )

    policy = Policy.allow_all()

    @policy.rule
    def no_enterprise(effect, ctx):
        if effect.args.get("tier") == "enterprise":
            return Deny("enterprise tier off-limits")
        return Allow()

    with dry_run({"sql": SQLiteAdapter(conn)}, policy=policy) as ctx:
        update_user(user_id=1, tier="basic")
        update_user(user_id=2, tier="enterprise")  # would normally raise
        update_user(user_id=3, tier="basic")

    result = ctx.result
    print(f"  journal materialised: {len(result.journal)} effects")
    print(f"  is_clean:             {result.is_clean}")
    print(f"  deny verdicts:")
    for v in result.policy_verdicts:
        if not v.allow:
            print(f"      where={v.where!r} rule={v.rule_name!r} "
                  f"effect_index={v.effect_index} reason={v.reason!r}")
    print(f"  SQL rows after txn:   "
          f"{list(conn.execute('SELECT * FROM users'))}")
    conn.close()


def scenario_3_world_unchanged() -> None:
    _banner("3. World-state assertion — SQL + FS bit-identical before/after")
    REGISTRY.clear()

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('pre-existing')")
    tmpdir = Path(tempfile.mkdtemp(prefix="pherix-dryrun-3-"))
    (tmpdir / "before.txt").write_bytes(b"original")

    pre_rows = list(conn.execute("SELECT * FROM notes"))
    pre_fs = _fs_snapshot(tmpdir)
    print(f"  before SQL: {pre_rows}")
    print(f"  before FS:  {pre_fs}")

    @tool(resource="sql")
    def add_note(conn, body):
        conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))

    @tool(resource="fs")
    def write_file(fs: FsHandle, path: str, data: bytes):
        fs.write(path, data)

    notify_fires: list[str] = []

    @tool(resource="http", reversible=False, injects_handle=False)
    def notify(channel):
        notify_fires.append(channel)

    with dry_run(
        {
            "sql": SQLiteAdapter(conn),
            "fs": FilesystemAdapter(tmpdir),
            "http": HTTPAdapter(),
        }
    ):
        add_note(body="dry-1")
        write_file(path="new.txt", data=b"new-content")
        add_note(body="dry-2")
        notify(channel="ops")

    post_rows = list(conn.execute("SELECT * FROM notes"))
    post_fs = _fs_snapshot(tmpdir)
    print(f"  after SQL:  {post_rows}")
    print(f"  after FS:   {post_fs}")
    print(f"  SQL identical?  {pre_rows == post_rows}")
    print(f"  FS identical?   {pre_fs == post_fs}")
    print(f"  notify_fires:   {notify_fires}  (irreversibles never fired)")
    conn.close()


if __name__ == "__main__":
    scenario_1_clean_mixed_txn()
    scenario_2_policy_denial_captured()
    scenario_3_world_unchanged()
    print()
    print("Slice 7 demo done.")
