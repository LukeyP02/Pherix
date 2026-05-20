# Audit dogfood — concurrent reconciliation, attributed and isolated

Two real reconciliation agents run **concurrently** against one seeded SQLite
ledger, under two `client_id`s. The payoff is the audit read *afterwards*: every
adjustment is attributed to the agent that posted it, the source entries are
uncorrupted (isolation held), and the whole thing is queryable as a per-client
compliance view.

```
python -m examples.dogfood.audit
```

(Needs an Anthropic key — see `examples/dogfood/README.md`. The offline test
`tests/test_dogfood_audit.py` drives the same composition with mocked clients.)

## What it proves

- **Attribution.** Each agent runs under a distinct `client_id`. The audit
  journal stamps that onto every transaction row (`transactions.client_id`), and
  the dogfood mirrors it onto every ledger row the agent writes
  (`adjustments.client_id` / `flags.client_id`) via a per-thread contextvar — so
  attribution holds even if the model never passes a `client_id` argument.
- **Isolation.** Each write declares its keys through `execute_isolated(...)`, so
  Pherix journals read/write versions per `(resource, key)`. If both agents touch
  the same entry row, the **commit-time conflict diff** catches the lost update.
- **Compliance view.** After both threads join, a third `AuditJournal` handle on
  the main thread folds the journal into a per-`client_id` view.

## Concurrency model (the load-bearing constraint)

A Pherix `TxnContext` is single-thread-owned (the runtime guards against
cross-thread use). So each agent opens its **own** `agent_txn` in its **own**
thread with its **own** `SQLiteAdapter` connection. The shared state is the
on-disk *files* (ledger + audit), never a Python object.

`AuditJournal` wraps a `sqlite3` connection opened with the default
`check_same_thread=True`, so **one `AuditJournal` instance cannot cross threads**.
Each agent thread therefore constructs its **own** `AuditJournal(audit_path)`
pointed at the **same** on-disk audit file; SQLite serialises the writes at the
file level. The combined compliance view is read on the main thread through a
**third** `AuditJournal(audit_path)` handle after both threads have joined.

## Isolation policy choice: `Abort`

We use `Abort` (first-committer-wins). If both agents contend on the same entry,
the second to commit raises `IsolationConflict` and unwinds cleanly — the ledger
is never corrupted by a lost update; the conflict surfaces on `AgentRun.error`.
`Retry` is the wrong fit here: it only does real work under `run_txn` (a callable
Pherix can re-invoke), and the harness drives a model loop inside
`with agent_txn(...)`, where `Retry` degrades to `Abort` anyway. `Serialize`
would make the second agent block on the first; `Abort` is the honest,
inspectable contract for a demo whose point is *that the conflict is caught*.
The default `__main__` tasks point the two agents at disjoint entries, so the
common path is clean parallel work; the conflict path is exercised
deterministically in the offline test
(`test_two_reconcilers_on_same_entry_isolated_no_corruption`), which uses the
in-process nested-`agent_txn` arbitration shape (see below for why, not free
threads).

## Engine findings (surfaced building this dogfood)

Two real `core/` behaviours this dogfood hit — flagged to the orchestrator, not
worked around in `core/` (no `core/` change was made):

1. **Read-then-write the same isolation key in one transaction falsely
   conflicts.** A txn that records `reads=[("entries", N)]` (via `query_ledger`)
   and then `writes=[("entries", N)]` (via `post_adjustment`) raises
   `IsolationConflict("read v0, now v0")` at its own commit — a self-conflict.
   Root cause: `SQLiteAdapter.read_version` reads through a separate *meta*
   connection (added so cross-process bumps are visible at commit), but the
   txn's own `write_version` bump is uncommitted on the *main* connection, so
   the meta connection cannot see it. `check_conflicts` then compares
   `v_expected = version_after_my_write (1)` against `v_now = meta_read (0)` and
   fires. This breaks the *natural* reconciliation flow ("read an entry, then
   correct it" in one txn). We work around it at the dogfood layer:
   `post_adjustment` is the only entry-mutating tool and is used *write-only*
   (no preceding query of the same entry in the same txn); `flag_discrepancy`
   declares no entry write-key (a flag is a pure append), so "read then flag" in
   one txn is legal. A fix belongs in `core/`: `read_version` at commit-diff time
   should see the txn's own pending write (e.g. read through the main connection,
   or fold pending writes from the journal).

2. **Free-running concurrency on one SQLite file is racy.** Two genuinely
   concurrent agents on one WAL file (a) collide on the single write lock — and
   because the harness reports any tool exception back to the model as a
   *swallowed* `tool_result` error, a `SQLITE_BUSY` write silently vanishes while
   the txn still commits (mitigated here with `PRAGMA busy_timeout`); and
   (b) suffer cross-connection WAL visibility lag — a stale read can commit clean
   ~3% of the time because the reader's meta connection has not yet observed the
   other agent's committed version bump. The live `__main__` demo runs the
   threaded version (a demo, not a gate); the **offline test is deterministic**
   (sequential agents for attribution, in-process nested `agent_txn` for the
   conflict) so it never rides on these timings.

---

## Audit pillar wishlist (Phase-2 requirements input)

Building the compliance view on top of **today's** `AuditJournal` API surfaced
concrete gaps. The current API is keyed entirely by `txn_id`
(`get_transaction(txn_id)`, `get_effects(txn_id)`) — there is no client-centric
or cross-transaction query. Every gap below forced us to either reach into the
private `_conn` or do client-side filtering that the store should do:

1. **`get_transactions()` / list-all.** There is no way to enumerate
   transactions. To find "which txns exist" we had to run
   `SELECT txn_id FROM transactions` against the private connection. A
   compliance tool should never touch `_conn`.

2. **`get_transactions_by_client(client_id)`.** The single most-wanted query:
   "show me everything `auditor-a` did." Today we list all txn_ids, fetch each
   transaction, and filter by `client_id` in Python — O(all transactions) for
   one client's view.

3. **`get_effects_by_client(client_id)`** (or `get_effects(txn_id=..., client_id=...)`).
   "Every adjustment this client posted, across all its transactions" should be
   one call. Today it's: filter txns by client → loop → `get_effects` per txn →
   concatenate.

4. **Filter by tool / resource / status.** "All `post_adjustment` effects" or
   "all effects still `STAGED`" needs a `WHERE tool = ?` / `WHERE status = ?`.
   Today every effect query is `txn_id`-scoped only.

5. **Diff two clients' adjustments.** A reconciliation/audit review wants "what
   did A change vs what did B change, on the same entries" — a set-difference
   over two clients' write-effects. No primitive exists; we'd assemble both
   client views and diff in Python.

6. **Time-range / `created_at` filtering.** Compliance views are usually scoped
   to a period ("all transactions in May"). The `created_at` / `updated_at`
   columns exist but there is no query that filters on them.

7. **A read-only handle / cursor.** The compliance view is a *read*, but
   `AuditJournal` only opens a read-write connection and runs DDL on init. A
   `AuditJournal.read_only(path)` (or a query object that doesn't `executescript`
   the schema) would let a separate auditing process attach without risk of
   mutating the store — and would sidestep the `check_same_thread` friction by
   making the read path explicitly concurrent-safe.

**The shape Phase-2 should ship:** a small query surface on `AuditJournal`
(`get_transactions(client_id=None, since=None, until=None)`,
`get_effects(txn_id=None, client_id=None, tool=None, status=None)`,
`diff_clients(a, b)`), plus a read-only opener. Everything here is a *fold or
diff over the journal* — exactly the project's core mental model — so it's
additive query surface over the existing tables, not a schema change.
