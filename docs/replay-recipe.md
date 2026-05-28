# Replay recipe

`replay()` re-fires a recorded transaction's effect journal against fresh
adapters and checks that each result is identical to the original. It is
useful for two scenarios:

- **Verifying determinism** — after restoring a backup, after a code change,
  or after suspecting a data-integrity issue, confirm the journal still
  reproduces the same results.
- **Disaster recovery** (`mode="reconstruct"`) — replay the journal against an
  empty database to rebuild the world the transaction described, accepting
  whatever today's tools produce as the new state.

## Why replay needs your adapters and tool registrations

Pherix records *what* each tool was called with and *what result it produced*.
On replay it calls the tool again, compares results, and reports any mismatch.
But Pherix has no idea where your database lives, what credentials your HTTP
client needs, or which filesystem root you want to write into — those are
environment specifics the operator owns.

You must therefore supply two things before calling `replay()`:

1. **Fresh adapters** — the same adapter *types* as the original run, pointed
   at whatever substrate you want to replay against.
2. **The same `@tool` registrations** — every tool name that appears in the
   source journal must be registered in the calling process. If any tool is
   absent, `replay()` raises `RuntimeError` with a message naming the missing
   tool and telling you to import the module that defines it.

Both of these have to be set up *before* the `replay()` call; there is no lazy
resolution.

## The journal default path

By default an `agent_txn` call with no explicit `audit=` opens the journal at
`~/.pherix/journal.db` (or `$PHERIX_JOURNAL` if that environment variable is
set). You can open the same file to replay any transaction that was recorded
there:

```python
from pherix import AuditJournal

audit = AuditJournal("~/.pherix/journal.db")
# or: audit = AuditJournal.default()
```

`AuditJournal.in_memory()` is the explicit ephemeral opt-out — in-memory
journals are not accessible across processes.

## Verify vs reconstruct

**`mode="verify"` (default):** each effect is re-fired against the fresh
adapters and the result is compared to the recorded result using the tool's
registered comparator (JSON-string equality by default). A mismatch is a
*divergence*. With `raise_on_divergence=True` (the default) the first
divergence raises `ReplayDivergence`; the exception carries a `ReplayResult`
with per-effect detail. With `raise_on_divergence=False` divergences are
collected in `result.divergences` without raising.

**`mode="reconstruct"`:** results are accepted as the new world's state — no
comparison is performed. The transaction is committed on the fresh adapters,
rebuilding whatever state the original journal described. Use this for
disaster-recovery replay where you do not expect (or need) bit-for-bit
identity.

## Irreversible effects

Effects marked `reversible=False` (HTTP calls, email sends, payment charges)
are **never re-fired** on replay under either mode. The journal entry is the
witness. Replay reuses the recorded result and marks the outcome
`skipped_idempotent`. This is intentional — Pherix cannot honestly snapshot an
external API call, and re-firing it would double-bill the world.

## Minimal working example

The full runnable version lives in `examples/replay_demo.py`.

```python
import sqlite3
from pherix import AuditJournal, SQLiteAdapter, agent_txn, replay, tool

@tool(resource="sql")
def insert_user(conn, name: str) -> str:
    conn.execute("INSERT INTO users (id, name) VALUES (NULL, ?)", (name,))
    return name

def _fresh_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    return conn

# --- 1. record ---
source_audit = AuditJournal("/tmp/my_journal.db")
with agent_txn({"sql": SQLiteAdapter(_fresh_db())}, audit=source_audit) as ctx:
    insert_user(name="alice")
txn_id = ctx.txn_id

# --- 2. replay ---
result = replay(
    txn_id,
    {"sql": SQLiteAdapter(_fresh_db())},   # fresh adapters, same schema
    source_audit=source_audit,
    mode="verify",
)
print(result.status)   # "success"
source_audit.close()
```

Key points from the example:

- The `@tool` definition is present in the same process as the `replay()` call.
- The fresh adapter is a *new* connection object, not the one the source run
  used.
- `source_audit` is opened from the same path the original `agent_txn` wrote to.
- `ctx.txn_id` is the only handle you need to identify the source transaction;
  keep it or look it up from the journal if you did not capture it at record time.

## ReplayResult shape

```
result.status           "success" | "divergence" | "failure"
result.mode             "verify" | "reconstruct"
result.source_txn_id    the txn_id you passed in
result.replay_txn_id    a new txn_id for this replay run
result.outcomes         list[EffectOutcome] — one per effect
result.divergences      list[EffectOutcome] — subset of outcomes where status="divergence"
result.isolation_conflicts  list — non-empty flags Slice-4 contract leakage
```

Each `EffectOutcome` carries `.tool`, `.index`, `.status`, `.recorded_result`,
and `.replayed_result`.
