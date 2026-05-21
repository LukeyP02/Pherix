# Adapter & compensator catalog — the breadth pass

This note is for an operator wiring Pherix onto a real stack. It maps every
adapter and compensator shipped in `adapter-compensator-base` to the **axis** it
fills, states honestly what each can and cannot undo, and records the few sharp
edges worth knowing before you build against them.

## The two axes this fills

Recall the four axes. This task widens two of them:

- **Adapters** — *how do we undo what **can** be undone?* A `(snapshot, apply,
  restore)` triple over a resource. The backend does the undo; the adapter is the
  honest seam. `supports_rollback()` is the discipline that keeps breadth honest.
- **Compensators** — *how do we undo what **can't**?* A registered tool that is
  the semantic left-inverse of an irreversible action (`compensator ∘ action ≈
  identity`), fired on rollback.

The boundary between them is exactly `supports_rollback()`. Two undo mechanisms,
not one — state rollback (snapshot, then revert) versus semantic inverse (run an
opposite action). They meet only at the journal's backward fold.

## Adapters shipped

### Reversible — the snapshot/savepoint lane (`supports_rollback() → True`)

| Adapter | Backend | How it rolls back | Verified offline here? |
|---|---|---|---|
| `PostgresAdapter` | PostgreSQL (psycopg 3) | real `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` — same shape as SQLite | **Yes** — against live local PG 17 |
| `MySQLAdapter` | MySQL/InnoDB (pymysql) | `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` | Server-backed tests skip (no live MySQL); pure helpers run |
| `MongoAdapter` | MongoDB (pymongo) | **document-snapshot**: capture touched docs by `_id`, write them back / delete on restore | Yes — via `mongomock` |
| `S3Adapter` | S3 / object storage (boto3) | **object-snapshot**: capture bytes (or absence) of touched keys, put back / delete on restore | Yes — via `moto` |
| `RedisAdapter` | Redis (redis-py) | **key-snapshot** via `DUMP`/`RESTORE` (preserves type + TTL); delete if key was absent | Yes — via `fakeredis` |

The two SQL adapters are correct *by construction* — the database does the
rollback. The other three have no native savepoint, so reversibility is the same
trick the filesystem adapter uses: snapshot the touched state before mutating,
write it back on restore. That means **they must know which targets an effect
touches** — read off `effect.args` by convention:

- S3: `args["key"]` and/or `args["keys"]`; one adapter == one bucket.
- Redis: `args["key"]` and/or `args["keys"]`.
- Mongo: `args["collection"]` + `args["doc_id"]`, or `args["docs"]` (a list of
  `{"collection", "doc_id"}`) for multi-document effects.

### Irreversible — the staged/compensated lane (`supports_rollback() → False`)

| Adapter | Backend | Undo path |
|---|---|---|
| `RESTAdapter` | any REST / GraphQL API | none native — stages to commit; undo via a registered compensator |
| `MQAdapter` | any publish/pub-sub broker | none native (you cannot un-send) — undo via a tombstone/cancel compensator |

These behave exactly like `HTTPAdapter`: the effect does **not** fire at
stage-time; it is recorded as intent and fired only during `commit()`. `snapshot`
/`restore` raise `IrreversibleAdapterError` on purpose — honesty over pretence.

Each ships a **harness** so a SaaS API or broker becomes a Pherix tool in one call:

```python
from pherix.core.adapters.rest import rest_tool, graphql_tool
from pherix.core.adapters.messagequeue import publish_tool, tombstone_compensator

# REST: transport is an injectable callable (defaults to a stdlib urllib client)
charge = rest_tool("charge", method="POST", url="https://api.acme.test/charge",
                   transport=my_transport, compensator="refund")
# GraphQL is just a POST of {query, variables}
mutate = graphql_tool("mutate", url="https://api.acme.test/graphql",
                      query="mutation($id:ID!){ ... }", transport=my_transport)

# MQ: pair a publish with a tombstone on the same topic
publish = publish_tool("publish", broker=my_broker, compensator="tombstone")
tomb    = tombstone_compensator("tombstone", broker=my_broker)
```

## Compensator catalog

Each entry is a **factory** `register_<pair>(client, *, resource="<domain>") ->
(action, compensator)`. The `client` is duck-typed — you inject your real Stripe /
GitHub / etc. client; the kernel imports none of them. Pair them via the existing
`@tool(compensator=...)` seam; the action declares its inverse by name.

| Domain | Action → inverse | Reverses by (the shared arg key) |
|---|---|---|
| payments | `charge → refund` | `idempotency_key` |
| payments | `payout → reverse_payout` | `payout_id` |
| identity | `invite → revoke_invite` | `invite_id` |
| identity | `grant_role → revoke_role` | `(principal, role)` |
| identity | `send_email → ⛔ gate` | — (cannot unsend) |
| provisioning | `create_resource → delete_resource` | `resource_id` |
| provisioning | `scale_up → scale_down` | `(target, from_replicas)` |
| saas (GitHub) | `create_pr → close_pr` | `(repo, branch)` |
| saas (GitHub) | `add_label → remove_label` | `(repo, issue, label)` |
| saas (Slack) | `post_message → delete_message` | `(channel, ts)` |
| saas (Stripe) | `create_customer → delete_customer` | `customer_id` |
| saas (SendGrid) | `add_contact → remove_contact` | `(list_id, email)` |
| saas (Twilio) | `send_sms → ⛔ gate` | — (cannot unsend) |
| saas (Jira) | `create_issue → delete_issue` | `issue_key` |

### The one load-bearing fact about the seam

When the runtime fires a compensator on rollback, it hands it **the original
action's args, not the action's return value**. So every pair reverses off a key
that is present in the *args* — the standard idempotency-key pattern (the caller
chooses `idempotency_key` / `resource_id` / `invite_id` and both the action and
its inverse key off it). `scale_up → scale_down` is the instructive case: the
*prior* replica count is carried in the args (`from_replicas`) so the inverse can
restore the exact pre-state, since it never sees the action's result.

For genuinely un-undoable actions (`send_email`, `send_sms`) there is no inverse,
so they ship with **no compensator** — they *gate* at commit (`commit()` blocks
until `approve_irreversible()`). The honest "undo" for an unsendable thing is to
not send it without a human in the loop.

## Sharp edges

- **REST compensator args are nested.** `rest_tool` / `graphql_tool` give their
  tool a `**kwargs` signature, so the journal records the call-time kwargs under a
  single `kwargs` key. A compensator paired with a `rest_tool` therefore sees its
  args as `{"kwargs": {...}}`, not flattened. The MQ harness uses explicit
  `topic`/`message` params and stays flat. If you need flat REST compensator args,
  write the tool with explicit params rather than the generic harness.
- **Postgres/MySQL omit the SQLite `_pherix_intents` cross-process ledger** — that
  was a single-host SQLite-specific hack. Real MVCC + row locking covers it, so
  cross-process isolation is delegated to the engine diff + the DB's own locking.
- **Postgres/MySQL do not yet implement `StateDiffable`** (`state_baseline` /
  `state_diff`). SQLite has it for dry-run structural diffs; the new SQL adapters
  do not. Dry-run still works (the journal records intent); only the row-level
  added/modified/deleted delta is SQLite-only for now.
- **Connections must be autocommit.** `PostgresAdapter` / `MySQLAdapter` take an
  already-open connection and drive every BEGIN/SAVEPOINT/COMMIT/ROLLBACK
  themselves — so `conn.autocommit = True` (psycopg) / `conn.autocommit(True)`
  (pymysql) is required, as documented on each class.

## Running the tests

```bash
pip install -e '.[test-adapters]'   # moto / fakeredis / mongomock + drivers
pytest -q tests                     # S3, Redis, Mongo round-trips run offline
PHERIX_TEST_PG_DSN='dbname=pherix_test' pytest -q tests/test_adapters_postgres.py
PHERIX_TEST_MYSQL_HOST=... pytest -q tests/test_adapters_mysql.py   # else skips
```

Backend-absent adapter tests skip cleanly; the kernel still imports with zero
third-party packages installed.
