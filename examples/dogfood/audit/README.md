# Audit dogfood — concurrent reconciliation, attributed and isolated

Two real reconciliation agents run **concurrently** against one seeded SQLite
ledger, under two `client_id`s. The payoff is the audit read *afterwards*: every
adjustment is attributed to the agent that posted it, the source entries are
uncorrupted (isolation held), the books were genuinely reconciled, and the whole
thing is queryable as a per-client compliance view.

```
python -m examples.dogfood.audit       # the real-agent run (needs a key)
```

(Needs an Anthropic key — see `examples/dogfood/README.md`.
`tests/test_dogfood_audit.py` is the **mechanism test** — mocked clients,
deterministic, CI — that guards the same composition; it is *not* a real-agent
run.)

## The genuine task (read the entry, then correct that same entry)

The ledger is seeded with a **real arithmetic imbalance**: a trial balance whose
signed entries should sum to zero (debits = credits) but do not, because two
entries are overstated against their expected control values. Each agent is
handed the expected amounts for its entries, *reads the live amounts* through
Pherix, works out the correcting deltas, and **books a correcting adjustment
against each wrong entry** so the corrected balance reaches zero. Success is
checkable — `ledger_balance(db) == 0` — and depends on what the agent actually
computes, not on a scripted sequence. A real agent can flip a sign, miss an
entry, or over-correct; that variance is the honest signal.

This is the natural reconciliation flow — *read entry N, then correct entry N in
the same transaction*. It used to trip a false self-conflict (finding #1 below);
that is now **fixed on main**, so the dogfood does the genuine thing rather than
routing corrections through a suspense account to dodge the bug.

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
deterministically in the mechanism test
(`test_reviewer_and_corrector_on_same_entry_isolated_no_corruption`), which uses
the in-process nested-`agent_txn` arbitration shape (see below for why, not free
threads). The conflict it constructs is the genuine one this engine detects: one
reconciler *reads* entry N and another *writes* it first, so the slow one's read
goes stale and `Abort` unwinds it. (Two pure writers never conflict in this
optimistic model — a conflict always requires a stale read.)

## Engine findings (surfaced building this dogfood)

Two real `core/` behaviours this dogfood hit. Finding #1 has since been **fixed
on main**; finding #2 is an inherent property of free SQLite concurrency that
the demo accommodates.

1. **Read-then-write the same isolation key in one transaction falsely
   conflicted — now fixed.** A txn that recorded `reads=[("entries", N)]` (via
   `query_ledger`) and then `writes=[("entries", N)]` (via `post_adjustment`)
   used to raise `IsolationConflict("read v0, now v0")` at its own commit — a
   self-conflict. Root cause: `SQLiteAdapter.read_version` read through a
   separate *meta* connection (so cross-process bumps are visible at commit),
   but the txn's own `write_version` bump was uncommitted on the *main*
   connection, so the meta connection could not see it; `check_conflicts` then
   compared `v_expected = 1` against `v_now = 0` and fired. The fix (merged,
   `feat/isolation-self-write`): the commit-time diff distinguishes
   own-write-visible adapters from committed-only ones via
   `reads_committed_only()` — a committed-only adapter compares the committed
   base captured *at read* against now, so a txn's own pending write no longer
   looks like someone else's bump — with a 450-line on-disk test matrix
   (`test_isolation_self_write.py`) closing the coverage gap. **Because of this
   fix, the audit dogfood now reads an entry and books the correction against
   that same entry** — the natural flow — rather than indirecting through a
   suspense account. The cross-txn conflict path (one txn's read goes stale
   because another wrote the key) is unchanged and still real.

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
