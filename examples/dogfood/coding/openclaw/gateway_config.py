"""``build_gateway()`` for OpenClaw's MCP registry — Part B1.

Run it as the MCP server OpenClaw spawns::

    python -m pherix.frontends.proxy examples.dogfood.coding.openclaw.gateway_config

and register that command in ``~/.openclaw/openclaw.json`` (see ``openclaw.json``
in this directory). Every tool OpenClaw calls *through MCP* then runs inside a
Pherix transaction: snapshotted, policy-checked, gated, audited — with the
session's ``client_id`` set to OpenClaw's handshake identity.

The governance shown here is deliberately small but real:

  * ``add_task`` / ``rename_task`` are **reversible** SQL writes OpenClaw may
    make — they journal, apply live, and roll back if the transaction unwinds.
  * ``clear_tasks`` is the destructive tool. The OpenClaw policy **denies** it,
    so when the model tries to wipe the table the call is refused at stage-time,
    nothing is journalled, and the model reads the refusal and adapts.
  * a **count cap** keeps a runaway agent from inserting without bound.
  * the **default policy is deny-all** — an MCP client whose identity the
    operator did not explicitly grant runs under the floor, never unpoliced.

The factory is import-time-significant: importing this module is what registers
the ``@tool`` functions into the global registry, so ``tools/list`` enumerates
exactly these three. The SQLite DB and audit journal default to in-memory (so
the module is runnable and offline-testable as-is); point them at real files for
a persistent run via ``PHERIX_OPENCLAW_DB`` / ``PHERIX_OPENCLAW_AUDIT``.
"""

from __future__ import annotations

import os
import sqlite3

from pherix.core.adapters.sql import SQLiteAdapter
from pherix.core.audit import AuditJournal
from pherix.core.policy import Allow, Cap, Deny, Policy
from pherix.core.tools import tool
from pherix.frontends.proxy import PherixGateway

from examples.dogfood.coding.openclaw import OPENCLAW_IDENTITY

_SCHEMA = "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT)"


@tool(resource="sql")
def add_task(conn, title):
    """Add a task by title (reversible — journalled and rolled back on unwind)."""
    conn.execute("INSERT INTO tasks (title) VALUES (?)", (title,))
    return f"added task {title!r}"


@tool(resource="sql")
def rename_task(conn, task_id, title):
    """Rename a task by id (reversible)."""
    conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, task_id))
    return f"renamed task {task_id} -> {title!r}"


@tool(resource="sql")
def clear_tasks(conn):
    """Delete every task (destructive — the OpenClaw policy denies this)."""
    conn.execute("DELETE FROM tasks")
    return "cleared all tasks"


def no_destructive_clear(effect, ctx) -> Allow | Deny:
    """Rule: OpenClaw may add/rename tasks but must not wipe the table.

    The canonical "can change state, cannot destroy it wholesale" boundary —
    expressed as a fold over the journalled effect's tool name, so ``clear_tasks``
    is recorded GATED rather than silently executed.
    """
    if effect.tool == "clear_tasks":
        return Deny("clear_tasks is destructive and forbidden for OpenClaw")
    return Allow()


def openclaw_policy() -> Policy:
    """The policy OpenClaw's MCP session runs under.

    Allows the two reversible writes, denies the destructive clear, and caps
    inserts so a runaway loop cannot hammer the table.
    """
    return Policy.with_rules(
        rules=[no_destructive_clear],
        caps=[Cap.count(tool="add_task", max=25)],
    )


def build_gateway() -> PherixGateway:
    """Construct the gateway OpenClaw's MCP client connects to.

    The SQLite connection is autocommit (``isolation_level=None``) so the Pherix
    adapter owns every BEGIN / SAVEPOINT / COMMIT / ROLLBACK. The OpenClaw
    identity maps to :func:`openclaw_policy`; every other identity (including a
    client that sends none) falls back to the deny-all floor.
    """
    db_path = os.environ.get("PHERIX_OPENCLAW_DB", ":memory:")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(_SCHEMA)

    audit_path = os.environ.get("PHERIX_OPENCLAW_AUDIT")
    audit = AuditJournal(audit_path) if audit_path else AuditJournal.in_memory()

    return PherixGateway(
        adapters={"sql": SQLiteAdapter(conn)},
        policies={OPENCLAW_IDENTITY: openclaw_policy()},
        # Deny-all floor: an unrecognised MCP client never runs unpoliced.
        default_policy=Policy(allow=set()),
        audit=audit,
    )
