"""Audit dogfood — two reconciliation agents racing on one ledger, attributed.

A real model is given three Pherix-wrapped tools over a seeded SQLite ledger:

- ``query_ledger`` — read ledger rows (reads journalled for isolation).
- ``post_adjustment`` — write a correcting entry (writes journalled).
- ``flag_discrepancy`` — record a flag (a write into the ``flags`` table).

The dogfood runs the agent **twice concurrently** under two ``client_id``s,
each in its own thread with its own ``SQLiteAdapter`` connection to the same
on-disk ledger file. The payoff is the read *afterwards*: every adjustment is
attributed by ``client_id``, the ledger is uncorrupted (isolation held — if the
two agents touch the same row the commit-time conflict diff catches it), and the
whole thing is queryable as a per-client compliance view.

Threading model (the load-bearing constraint):

- A Pherix ``TxnContext`` is single-thread-owned (the runtime guards against
  cross-thread use), so each concurrent agent opens its OWN ``agent_txn`` in its
  OWN thread with its OWN connection. The *shared* state is the on-disk ledger
  file and the audit DB file — not any Python object.
- ``AuditJournal`` wraps a ``sqlite3`` connection opened with the default
  ``check_same_thread=True``, so a single ``AuditJournal`` instance **cannot**
  be passed across threads. We therefore give each agent thread its OWN
  ``AuditJournal(audit_path)`` pointed at the SAME on-disk audit file, and read
  the combined compliance view through a THIRD ``AuditJournal(audit_path)`` on
  the main thread after both threads have joined. SQLite serialises the writes
  at the file level; the audit DB ends up holding both transactions' rows.

This module owns only the tools, the run helpers, and the compliance-view
builder. The Anthropic loop itself lives in the read-only foundation
``harness.run_agent`` — we import it, we don't fork it.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Any, Callable

from pherix.core.adapters.sql import SQLiteAdapter, execute_isolated
from pherix.core.audit import AuditJournal
from pherix.core.tools import tool

from examples.dogfood.harness import AgentRun, run_agent
from examples.dogfood.infra import ScratchDB

# Default model inherited from the harness; named here so a real run can override.
DEFAULT_MODEL = "claude-sonnet-4-6"

# The seed ledger: a handful of entries, one of which is wrong on purpose so a
# reconciliation agent has something to correct. ``entries`` is the source of
# truth; ``adjustments`` is the append-only correction log (every row carries
# the ``client_id`` of the agent that posted it — provenance at the data layer,
# mirrored by the audit journal's per-transaction ``client_id``); ``flags``
# records discrepancies the agent could not (or chose not to) auto-correct.
LEDGER_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE entries (
    id      INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    amount  INTEGER NOT NULL
);
CREATE TABLE adjustments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id  INTEGER NOT NULL,
    delta     INTEGER NOT NULL,
    reason    TEXT NOT NULL,
    client_id TEXT
);
CREATE TABLE flags (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id  INTEGER NOT NULL,
    note      TEXT NOT NULL,
    client_id TEXT
);
INSERT INTO entries (id, account, amount) VALUES
    (1, 'cash',       1000),
    (2, 'receivable',  500),
    (3, 'payable',    -300),
    (4, 'inventory',   750);
"""


# --- the agent's @tool surface ---------------------------------------------
#
# Registered once per process (the REGISTRY is process-global). Both concurrent
# agents share the same three registered tools; the ``client_id`` that
# attributes each call is carried by the transaction, not the tool.


# The write tools attribute rows via ``_ACTIVE_CLIENT`` — a contextvar set per
# agent thread (contextvars are copied into a new thread's empty default, so
# each thread sets its own) — so attribution does not depend on the model
# passing the right ``client_id`` argument. The tools are registered exactly
# once into the process-global ``REGISTRY``; both concurrent agents reuse them.
_ACTIVE_CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pherix_audit_active_client", default=None
)


@tool(resource="sql")
def query_ledger(conn, entry_id):
    """Read one ledger entry by id; returns its id, account and amount."""
    # Keyed by entry id so the read participates in the same MVCC key space as
    # post_adjustment's write — a stale read of entry N conflicts with a
    # committed adjustment to entry N at commit-time.
    cur = execute_isolated(
        conn,
        "SELECT id, account, amount FROM entries WHERE id = ?",
        (entry_id,),
        reads=[("entries", entry_id)],
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "account": row[1], "amount": row[2]}


@tool(resource="sql")
def post_adjustment(conn, entry_id, delta, reason):
    """Post a correcting adjustment of `delta` against ledger entry `entry_id`."""
    client_id = _ACTIVE_CLIENT.get()
    execute_isolated(
        conn,
        "INSERT INTO adjustments (entry_id, delta, reason, client_id) "
        "VALUES (?, ?, ?, ?)",
        (entry_id, delta, reason, client_id),
        # The entry row is the row this adjustment corrects: the version
        # side-table key is ("entries", entry_id), shared with query_ledger's
        # read key, so a concurrent write to the same entry moves the version a
        # stale reader recorded.
        writes=[("entries", entry_id)],
    )
    return f"adjustment posted to entry {entry_id} (delta={delta})"


@tool(resource="sql")
def flag_discrepancy(conn, entry_id, note):
    """Flag a discrepancy on ledger entry `entry_id` for human review."""
    client_id = _ACTIVE_CLIENT.get()
    # A flag is a pure APPEND to the flags log — it does not mutate the entry,
    # so it declares no isolation write-key on ("entries", entry_id). That keeps
    # the realistic flow "read an entry, then flag it" inside one transaction
    # legal: a read-only-then-flag txn has read_keys but no conflicting write to
    # the same key, so it commits clean. (Contrast post_adjustment, which DOES
    # mutate the entry's reconciled state and so declares the entry write-key —
    # see the README's note on the read-then-write-same-key engine limitation.)
    execute_isolated(
        conn,
        "INSERT INTO flags (entry_id, note, client_id) VALUES (?, ?, ?)",
        (entry_id, note, client_id),
    )
    return f"discrepancy flagged on entry {entry_id}"


AUDIT_TOOLS: list[Callable[..., Any]] = [
    query_ledger,
    post_adjustment,
    flag_discrepancy,
]

SYSTEM_PROMPT = (
    "You are a ledger reconciliation auditor. You have tools to read a ledger "
    "entry by its id, post correcting adjustments against an entry, and flag "
    "discrepancies for human review. Read the entries you are asked about. If "
    "an entry is clearly wrong, post ONE adjustment for it. If you are merely "
    "unsure, flag it instead. Important: do not both read AND post an "
    "adjustment for the very same entry id in this session — decide from the "
    "values you are given. Keep your changes minimal and explain each "
    "adjustment's reason."
)


# --- running one agent (thread body) ---------------------------------------


@dataclass
class ClientRun:
    """One agent's outcome plus the ``client_id`` it ran under."""

    client_id: str
    run: AgentRun


def _run_one(
    *,
    db: ScratchDB,
    audit_path: str,
    client_id: str,
    task: str,
    model: str,
    client: Any,
    out: dict[str, ClientRun],
) -> None:
    """Thread body: one agent, its own connection, its own AuditJournal.

    Everything thread-affine (the SQLite ledger connection, the SQLiteAdapter
    bound to it, the AuditJournal handle) is constructed *inside this thread* —
    never handed in from the parent. The only shared things are the two on-disk
    file *paths* (ledger + audit), which SQLite serialises across connections.
    """
    token = _ACTIVE_CLIENT.set(client_id)
    conn = db.connect()
    # WAL allows one writer at a time; two concurrent reconcilers can collide on
    # the single write lock. Without a busy timeout the loser gets SQLITE_BUSY
    # immediately — and because the harness reports any tool exception back to
    # the model as a swallowed tool_result error, that write would silently
    # vanish while the txn still committed. A busy timeout makes the second
    # writer WAIT for the lock instead of failing, so both writes land. (This is
    # connection config the dogfood owns; nothing in core changes.)
    conn.execute("PRAGMA busy_timeout = 5000")
    audit = AuditJournal(audit_path)
    try:
        adapter = SQLiteAdapter(conn)
        # Isolation policy: Abort (first-committer-wins). The foundation
        # harness does not expose an ``isolation=`` argument, so it always
        # opens ``agent_txn`` with the engine default — which IS ``Abort``.
        # That happens to be exactly what we want: if two agents touch the
        # same entry row, the second to commit raises ``IsolationConflict``
        # and unwinds, so the ledger is never corrupted by a lost update; the
        # conflict surfaces on ``AgentRun.error``. Retry would be wrong here —
        # it only does real work under ``run_txn`` (a callable Pherix can
        # re-invoke), and the harness drives a model loop inside
        # ``with agent_txn(...)`` where Retry degrades to Abort anyway. (If a
        # future dogfood needs Serialize/Retry, the harness must grow an
        # ``isolation=`` passthrough — reported to the orchestrator.)
        run = run_agent(
            task=task,
            system=SYSTEM_PROMPT,
            tools=AUDIT_TOOLS,
            adapters={"sql": adapter},
            policy=None,
            client_id=client_id,
            model=model,
            client=client,
            audit=audit,
        )
        out[client_id] = ClientRun(client_id=client_id, run=run)
    finally:
        audit.close()
        conn.close()
        _ACTIVE_CLIENT.reset(token)


def run_two_agents(
    *,
    db: ScratchDB,
    audit_path: str,
    tasks: dict[str, str],
    model: str = DEFAULT_MODEL,
    clients: dict[str, Any] | None = None,
    sequential: bool = False,
) -> dict[str, ClientRun]:
    """Run two reconciliation agents and return their outcomes, keyed by client.

    ``tasks`` maps ``client_id -> task prompt`` (two entries). ``clients`` maps
    ``client_id -> Anthropic-compatible client``; when absent each agent
    lazy-constructs the real SDK (needs a key). The offline test injects mocks.

    Each agent runs in its own thread with its own ledger connection and its own
    ``AuditJournal(audit_path)`` to the shared on-disk audit file — see the
    module docstring for why the journal cannot be shared across threads.

    ``sequential`` (default ``False``) joins each agent's thread before starting
    the next — same per-thread isolation (own ``TxnContext``, own connection),
    but with a deterministic, non-overlapping order. The live ``__main__`` demo
    runs them genuinely concurrent (``sequential=False``); the offline
    attribution test runs them sequentially so its assertions don't depend on
    SQLite's cross-connection write-lock timing. (Free-running concurrency on
    one SQLite file is genuinely racy — see this package's README, "concurrency
    findings".)
    """
    clients = clients or {}
    out: dict[str, ClientRun] = {}
    threads = [
        threading.Thread(
            target=_run_one,
            kwargs=dict(
                db=db,
                audit_path=audit_path,
                client_id=client_id,
                task=task,
                model=model,
                client=clients.get(client_id),
                out=out,
            ),
        )
        for client_id, task in tasks.items()
    ]
    if sequential:
        for t in threads:
            t.start()
            t.join()
    else:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    return out


# --- the compliance view (read the audit AFTER both agents close) ----------


@dataclass
class ClientAuditView:
    """One client's slice of the post-run compliance view.

    ``txns`` is the list of transaction rows attributed to this ``client_id``;
    ``effects`` is every journalled effect across those txns; ``adjustments`` /
    ``flags`` are the ledger rows the client actually wrote (read straight from
    the ledger, attributed by the row's ``client_id`` column).
    """

    client_id: str
    txns: list[dict]
    effects: list[dict]
    adjustments: list[dict]
    flags: list[dict]


def compliance_view(
    *, audit_path: str, ledger_db: ScratchDB, client_ids: list[str]
) -> dict[str, ClientAuditView]:
    """Build a per-``client_id`` compliance view from the audit + ledger.

    Read on the MAIN thread, AFTER both agent threads have joined, through a
    fresh ``AuditJournal(audit_path)`` handle (the agents' own handles are
    closed). This is the "audit read afterward" that is the dogfood's payoff:
    every adjustment attributed, the ledger queryable as a compliance record.

    NOTE the seams we hit here are exactly the Phase-2 audit-pillar wishlist
    (see this package's README): ``AuditJournal`` today is keyed by ``txn_id``,
    so to get "all effects by ``client_id``" we have to scan transactions,
    filter by ``client_id`` ourselves, then re-fetch effects per txn. There is
    no ``get_transactions_by_client`` and no ``get_effects_by_client``.
    """
    audit = AuditJournal(audit_path)
    # Read the ledger through a FRESH connection, not the long-lived primary
    # ``ledger_db.conn``. Under WAL a connection can hold a read snapshot from
    # before the worker threads committed; a fresh autocommit connection always
    # sees the latest committed frames. (This was a real flaky-read race when
    # reading through the shared primary connection.)
    ledger = ledger_db.connect()
    try:
        # WISHLIST GAP #1: no list-all and no by-client query. We can only
        # `get_transaction(txn_id)`. We therefore read every txn_id straight off
        # the audit DB's `transactions` table via the private connection —
        # something a real compliance tool should not have to reach into.
        all_txn_ids = [
            r[0]
            for r in audit._conn.execute(  # noqa: SLF001 - wishlist gap, see README
                "SELECT txn_id FROM transactions ORDER BY created_at"
            ).fetchall()
        ]

        views: dict[str, ClientAuditView] = {}
        for cid in client_ids:
            txns = [
                t
                for txn_id in all_txn_ids
                if (t := audit.get_transaction(txn_id))
                and t["client_id"] == cid
            ]
            effects: list[dict] = []
            for t in txns:
                effects.extend(audit.get_effects(t["txn_id"]))
            adjustments = [
                {"id": r[0], "entry_id": r[1], "delta": r[2], "reason": r[3]}
                for r in ledger.execute(
                    "SELECT id, entry_id, delta, reason FROM adjustments "
                    "WHERE client_id = ? ORDER BY id",
                    (cid,),
                ).fetchall()
            ]
            flags = [
                {"id": r[0], "entry_id": r[1], "note": r[2]}
                for r in ledger.execute(
                    "SELECT id, entry_id, note FROM flags "
                    "WHERE client_id = ? ORDER BY id",
                    (cid,),
                ).fetchall()
            ]
            views[cid] = ClientAuditView(
                client_id=cid,
                txns=txns,
                effects=effects,
                adjustments=adjustments,
                flags=flags,
            )
        return views
    finally:
        ledger.close()
        audit.close()


def ledger_snapshot(ledger_db: ScratchDB) -> list[dict]:
    """The current ledger entries — proof the source rows are uncorrupted.

    Reads through a fresh connection (see ``compliance_view`` for why the
    long-lived primary connection can hold a stale WAL read snapshot).
    """
    conn = ledger_db.connect()
    try:
        return [
            {"id": r[0], "account": r[1], "amount": r[2]}
            for r in conn.execute(
                "SELECT id, account, amount FROM entries ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
