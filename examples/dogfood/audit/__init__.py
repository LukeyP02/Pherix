"""Audit dogfood — two reconciliation agents on one ledger, attributed + isolated.

A real model is given three Pherix-wrapped tools over a seeded SQLite ledger:

- ``query_ledger`` — read a ledger entry by id (reads journalled for isolation).
- ``post_adjustment`` — book a correcting adjustment (writes journalled).
- ``flag_discrepancy`` — record a flag for human review (an append to ``flags``).

The genuine task (read the entry, then correct that same entry)
---------------------------------------------------------------
The ledger is seeded with a **real arithmetic imbalance**: a trial balance whose
signed entries should sum to zero (debits = credits) but do not, because two
entries are overstated against their expected control values. A reconciliation
agent has to *read the live amounts*, compare them to the expected values it is
given, work out the correcting deltas, and **book a correcting adjustment
against the entry that is wrong** so the books balance. Success is checkable —
the corrected trial balance must reach zero — and depends on what the agent
actually computes, not on a scripted sequence. A real agent can get a sign
wrong, miss an entry, or over-correct; that variance is the honest signal.

This is the natural reconciliation flow — *read entry N, then correct entry N in
the same transaction*. It relies on Pherix's Slice-4 isolation handling a txn
that reads key ``("entries", N)`` and then writes the same key without a false
self-conflict. That used to misfire (the commit-time ``read_version`` could not
see the txn's own uncommitted write); it is **fixed on main** (the commit-time
diff reconciles own-write-visible vs committed-only adapters — see
``test_isolation_self_write.py``), so the dogfood now does the genuine thing
rather than routing corrections through a suspense account to dodge the bug.

The two-agent payoff: attribution + isolation
----------------------------------------------
The dogfood runs the agent **twice concurrently** under two ``client_id``s, each
in its own thread with its own ``SQLiteAdapter`` connection to the same on-disk
ledger file. Every adjustment is attributed by ``client_id`` (in the audit
journal *and* on the ledger row), the source entries are uncorrupted, and the
whole run is queryable as a per-client compliance view. Genuine isolation is
demonstrated deterministically in-process: two reconcilers contend on the same
entry — one reads it and corrects it, the other commits a correction to that
entry first, and the slow one's now-stale read is aborted at commit. See the
README for why the live threaded demo does not gate on free-running SQLite
concurrency.

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
from pherix.core.isolation import IsolationConflict
from pherix.core.runtime import agent_txn
from pherix.core.tools import REGISTRY, tool

from examples.dogfood.harness import AgentRun, run_agent
from examples.dogfood.infra import ScratchDB

# Default model inherited from the harness; named here so a real run can override.
DEFAULT_MODEL = "claude-sonnet-4-6"

# The seed ledger: a trial balance that SHOULD sum to zero but does not, because
# entries 2 (receivable) and 4 (inventory) are each overstated by 50 against
# their expected control values. A reconciler must read the actual amounts,
# compare to EXPECTED, and book a correcting delta against each wrong entry so
# the corrected balance reaches zero.
#
# ``entries`` is the source of truth; ``adjustments`` is the append-only
# correction log (every row carries the ``client_id`` of the agent that posted
# it — provenance at the data layer, mirrored by the audit journal's
# per-transaction ``client_id``); ``flags`` records discrepancies the agent
# could not (or chose not to) auto-correct.
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
    (1,  'cash',        1000),
    (2,  'receivable',   550),
    (3,  'payable',     -300),
    (4,  'inventory',    800),
    (5,  'equity',     -1950);
"""

# The expected (control) amounts for the seeded entries. Two are wrong on
# purpose: entry 2 is 550 but should be 500, entry 4 is 800 but should be 750.
# A correct reconciliation books -50 against entry 2 and -50 against entry 4, so
# the corrected trial balance (sum of entries + sum of adjustments) reaches 0.
EXPECTED_AMOUNTS = {1: 1000, 2: 500, 3: -300, 4: 750, 5: -1950}


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
    """Book a correcting adjustment of `delta` against ledger entry `entry_id`.

    A reconciler reads the entry it is checking and, if it is wrong, books the
    correction against that same entry in the same transaction — the natural
    flow, legal since the Slice-4 self-write fix on main. The write declares
    ``writes=[("entries", entry_id)]``, which shares the version side-table key
    with ``query_ledger``'s read key: if another open transaction has *read*
    this entry, the commit-time diff aborts that stale reader (the isolation
    conflict path) — but a txn reading and then writing its *own* entry no
    longer false-conflicts.
    """
    client_id = _ACTIVE_CLIENT.get()
    execute_isolated(
        conn,
        "INSERT INTO adjustments (entry_id, delta, reason, client_id) "
        "VALUES (?, ?, ?, ?)",
        (entry_id, delta, reason, client_id),
        # The write-key is the entry this adjustment corrects. The version
        # side-table key ("entries", N) is shared with query_ledger's read key,
        # so a write to an entry ANOTHER txn read moves the version that stale
        # reader recorded → it aborts at commit. A txn reading and then writing
        # its own entry N is fine (own write is visible to the commit-time diff).
        writes=[("entries", entry_id)],
    )
    return f"adjustment posted against entry {entry_id} (delta={delta})"


@tool(resource="sql")
def flag_discrepancy(conn, entry_id, note):
    """Flag a discrepancy on ledger entry `entry_id` for human review."""
    client_id = _ACTIVE_CLIENT.get()
    # A flag is a pure APPEND to the flags log — it does not mutate the entry,
    # so it declares no isolation write-key on ("entries", entry_id): a flag
    # records a discrepancy for human review rather than correcting it, so it
    # never contends with another agent's correction. (Contrast post_adjustment,
    # which declares an entry write-key.)
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
    "You are a ledger reconciliation auditor. A correct trial balance sums to "
    "zero. Some entries are overstated against their expected values and you "
    "must correct them. You have tools to read a ledger entry by its id, to "
    "book a correcting adjustment, and to flag a discrepancy for human review.\n\n"
    "For each entry you are asked to reconcile: read its actual amount, compare "
    "it to the expected amount you are given, and if they differ, book ONE "
    "correcting adjustment against that entry whose delta brings it back to its "
    "expected value (delta = expected - actual), stating the reason. If you are "
    "merely unsure about an entry, flag it for review instead of adjusting it. "
    "Keep your changes minimal and explain each adjustment."
)


# --- default two-agent tasks (shared by __main__ and the capture harness) ---

CLIENT_A = "auditor-a"
CLIENT_B = "auditor-b"


def default_tasks() -> dict[str, str]:
    """The two reconcilers' tasks: disjoint entry subsets, expected values given.

    Agent A owns entries {1, 2}, agent B owns {3, 4}; the seeded discrepancies
    sit on entries 2 and 4, so each agent has exactly one entry to correct. Each
    task hands the agent the expected (control) amounts and asks it to read the
    actual values, work out the correcting deltas, and book a correction against
    the wrong entry. Disjoint subsets make the common path clean parallel work;
    the deterministic conflict path is exercised in the mechanism test.
    """
    return {
        CLIENT_A: (
            f"Reconcile ledger entries 1 (cash, expected {EXPECTED_AMOUNTS[1]}) "
            f"and 2 (receivable, expected {EXPECTED_AMOUNTS[2]}). Read each "
            "entry's actual amount, and for any entry whose actual differs from "
            "expected, book a correcting adjustment against that entry to bring "
            "it back to the expected value. Flag anything you cannot resolve."
        ),
        CLIENT_B: (
            f"Reconcile ledger entries 3 (payable, expected {EXPECTED_AMOUNTS[3]}) "
            f"and 4 (inventory, expected {EXPECTED_AMOUNTS[4]}). Read each "
            "entry's actual amount, and for any entry whose actual differs from "
            "expected, book a correcting adjustment against that entry to bring "
            "it back to the expected value. Flag anything you cannot resolve."
        ),
    }


# --- balance / verdict helpers ---------------------------------------------


def ledger_balance(ledger_db: ScratchDB) -> int:
    """The corrected trial balance: sum of all entries + all adjustments.

    A genuinely-reconciled ledger sums to zero. Read through a fresh connection
    (a long-lived one can hold a stale WAL read snapshot — see ``ledger_snapshot``).
    """
    conn = ledger_db.connect()
    try:
        entries_total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM entries"
        ).fetchone()[0]
        adj_total = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM adjustments"
        ).fetchone()[0]
        return entries_total + adj_total
    finally:
        conn.close()


# --- the before/after: a contended entry, with isolation on vs off ----------
#
# The two-agent demo above runs DISJOINT entry subsets, so its common path is
# clean parallel work and the isolation payoff only shows under a genuine race.
# The before/after pair makes that payoff filmable and deterministic: put TWO
# reconcilers on the SAME entry, then compare the world with Pherix's isolation
# to the world without it. One -50 correction is needed; un-isolated, both
# agents book it (neither saw the other's write) and the entry over-corrects —
# the lost update. Isolated, the second committer's stale read is aborted, so
# exactly one correction lands.

# The single entry both reconcilers contend on: receivable, seeded at 550,
# expected 500 — exactly one -50 correction is needed.
CONTENDED_ENTRY = 2


def entry_effective_amount(ledger_db: ScratchDB, entry_id: int) -> int:
    """An entry's amount after its adjustments — ``entries.amount + Σ adjustments.delta``.

    A correctly-reconciled entry equals its :data:`EXPECTED_AMOUNTS` value.
    Over-correcting it — two agents each booking the same -50 because neither saw
    the other's write — pushes it past expected, which is the visible signature
    of the lost update. Read through a fresh connection (a long-lived one can
    hold a stale WAL read snapshot — see :func:`ledger_balance`).
    """
    conn = ledger_db.connect()
    try:
        base = conn.execute(
            "SELECT amount FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()[0]
        adj = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM adjustments WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()[0]
        return base + adj
    finally:
        conn.close()


@dataclass
class ContendedOutcome:
    """The result of one contended reconciliation — enough to judge either world.

    ``adjustments`` is every adjustment row on the contended entry as
    ``(entry_id, delta, client_id)``; ``effective_amount`` is the entry after
    those adjustments; ``conflict`` is whether the isolation engine aborted a
    stale reader (only meaningful when ``governed``). ``corrupted`` is the
    headline the demo films: the entry was pushed off its expected value.
    """

    governed: bool
    effective_amount: int
    expected_amount: int
    adjustments: list[tuple]
    conflict: bool

    @property
    def corrupted(self) -> bool:
        return self.effective_amount != self.expected_amount


def _contended_adjustments(ledger_db: ScratchDB, entry_id: int) -> list[tuple]:
    conn = ledger_db.connect()
    try:
        return conn.execute(
            "SELECT entry_id, delta, client_id FROM adjustments "
            "WHERE entry_id = ? ORDER BY id",
            (entry_id,),
        ).fetchall()
    finally:
        conn.close()


def run_contended_reconciliation(
    *,
    db: ScratchDB,
    audit_path: str,
    governed: bool,
    entry_id: int = CONTENDED_ENTRY,
) -> ContendedOutcome:
    """Two reconcilers race on ONE entry; ``governed`` decides whether it corrupts.

    Both auditors read ``entry_id`` (seeded 550) and each independently concludes
    it is overstated by 50, so each books a -50 correction. Exactly one is needed.

    ``governed=True`` is the deterministic Slice-4 conflict shape (the same one
    ``test_reviewer_and_corrector_on_same_entry_isolated_no_corruption`` proves):
    A reads the entry inside its transaction — the reviewer, about to book the
    same -50; while A is still open B books the -50 against the entry and commits,
    bumping the ``("entries", N)`` version; A reaches the end of its block and its
    commit-time diff sees its read went stale → ``Abort`` raises
    ``IsolationConflict`` and A unwinds, so A's redundant correction is never
    written. Net: B's single -50, the entry corrected to expected, attributed.

    (A is read-only here on purpose: two genuine *writers* on one SQLite file
    serialize at the SQLite write-lock layer — A's write against a snapshot B
    has since moved would raise ``database is locked`` *before* Pherix could
    arbitrate — so the deterministic in-process shape makes A the reviewer whose
    stale read is aborted. The before world below, being un-isolated autocommit,
    has no held transaction, so both writers genuinely land.)

    ``governed=False`` is the **before**: no transaction, no isolation. A reads
    (stale), B books -50, then A books -50 too — based on its now-stale read, A
    never saw B's correction — and both land. Net: two -50, the entry
    over-corrected to 450. That is the lost update the isolated path prevents.

    Single-threaded and deterministic in both worlds (the interleave is explicit,
    not a thread race), so it is safe to assert on in CI.
    """
    # The governed path drives the registered @tool wrappers (record_tool_call
    # looks the spec up in the process-global REGISTRY); the autouse test fixture
    # clears it, so re-register the specs if missing — same guard run_audit_batch
    # uses. The ungoverned path calls spec.fn directly and needs no registry.
    if governed:
        for wrapper in AUDIT_TOOLS:
            if wrapper.tool_spec.name not in REGISTRY:
                REGISTRY.register(wrapper.tool_spec)

    expected = EXPECTED_AMOUNTS[entry_id]
    reason = f"entry {entry_id} overstated by 50"
    conflict = False

    if governed:
        conn_a = db.connect()
        conn_b = db.connect()
        conn_a.execute("PRAGMA busy_timeout = 5000")
        conn_b.execute("PRAGMA busy_timeout = 5000")
        audit = AuditJournal(audit_path)
        try:
            ad_a = SQLiteAdapter(conn_a)
            ad_b = SQLiteAdapter(conn_b)
            a_token = _ACTIVE_CLIENT.set(CLIENT_A)
            try:
                with agent_txn({"sql": ad_a}, audit=audit, client_id=CLIENT_A):
                    # A is the reviewer: it reads the entry intending to book the
                    # same -50, recording the read version.
                    query_ledger(entry_id=entry_id)
                    # While A is open, B (the corrector) books the -50 against the
                    # entry and commits — bumping the ("entries", N) version.
                    b_token = _ACTIVE_CLIENT.set(CLIENT_B)
                    try:
                        with agent_txn(
                            {"sql": ad_b}, audit=audit, client_id=CLIENT_B
                        ):
                            post_adjustment(
                                entry_id=entry_id, delta=-50, reason=reason
                            )
                    finally:
                        _ACTIVE_CLIENT.reset(b_token)
                    # A reaches end-of-block; the commit-time diff sees A's read
                    # of the entry went stale → Abort → A unwinds, so A's
                    # redundant correction is never written.
            except IsolationConflict:
                conflict = True
            finally:
                _ACTIVE_CLIENT.reset(a_token)
        finally:
            audit.close()
            conn_a.close()
            conn_b.close()
    else:
        # No transaction, no isolation: both writes hit the ledger directly. We
        # dispatch spec.fn ourselves with the live connection — exactly what the
        # ungoverned harness path does — because the @tool wrapper outside
        # agent_txn would passthrough without injecting the connection.
        q_fn = query_ledger.tool_spec.fn
        p_fn = post_adjustment.tool_spec.fn
        conn_a = db.connect()
        conn_b = db.connect()
        conn_a.execute("PRAGMA busy_timeout = 5000")
        conn_b.execute("PRAGMA busy_timeout = 5000")
        a_token = _ACTIVE_CLIENT.set(CLIENT_A)
        try:
            q_fn(conn_a, entry_id=entry_id)  # A's stale read
            b_token = _ACTIVE_CLIENT.set(CLIENT_B)
            try:
                p_fn(conn_b, entry_id=entry_id, delta=-50, reason=reason)  # B
            finally:
                _ACTIVE_CLIENT.reset(b_token)
            p_fn(conn_a, entry_id=entry_id, delta=-50, reason=reason)  # A (stale)
        finally:
            _ACTIVE_CLIENT.reset(a_token)
            conn_a.close()
            conn_b.close()

    return ContendedOutcome(
        governed=governed,
        effective_amount=entry_effective_amount(db, entry_id),
        expected_amount=expected,
        adjustments=_contended_adjustments(db, entry_id),
        conflict=conflict,
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
    lazy-constructs the real SDK (needs a key). The offline mechanism test
    injects mocks.

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
