# Pherix — Auditable Agents

Database guarantees over an AI agent's real-world actions: **undo the reversible · gate the irreversible · audit everything.**

Pherix is a Python **library** that wraps your agent's tool-call layer and gives database-style guarantees — atomicity, isolation, capability enforcement, durability — over the *external side-effects* of the tool calls (DB writes, file writes, API calls). It does **not** run your agent or call any LLM. You keep your existing agent loop and model provider; Pherix sits underneath at the tool-call layer.

## Who it's for

Anyone shipping action-taking agents to production — where a tool call writes to a database, touches the filesystem, or fires an irreversible API request, and "the effect just happened" is not good enough.

## Quickstart

> Pre-release `0.0.0` — install from source. The wrap below is the real minimal one today; a sane-defaults, shorter wrap is coming.

> **Using a coding assistant?** Point Claude Code / Cursor / Aider at [`llms.txt`](llms.txt) — it's a complete, executable integration recipe (with the gotchas spelled out) written so an LLM can wrap your agent in Pherix correctly without you fighting the API.

**Install**

```bash
git clone https://github.com/LukeyP02/Pherix && cd Pherix
pip install -e .
```

**1 — Declare your tools with `@tool`.** Mark each side-effecting function with the resource it touches. The agent body that calls them stays transaction-unaware — just a plain loop.

```python
import sqlite3
from pherix import AuditJournal, SQLiteAdapter, agent_txn, tool

@tool(resource="sql")
def insert_user(conn, name, role):
    conn.execute("INSERT INTO users (name, role) VALUES (?, ?)", (name, role))
    return name

def my_agent(team):
    # a plain agent loop — never transaction-aware
    for name, role in team:
        insert_user(name=name, role=role)
```

**2 — Wrap the run in `agent_txn(...)`.** Pass your adapters. Reversible effects journal live and roll back on demand; leaving the block cleanly commits them.

```python
conn = sqlite3.connect("app.db", isolation_level=None)
audit = AuditJournal.in_memory()
adapters = {"sql": SQLiteAdapter(conn)}

with agent_txn(adapters, audit=audit) as txn:
    my_agent([("ada", "engineer"), ("grace", "scientist")])
    # caught a problem? roll the whole step back — nothing persisted:
    # txn.rollback()
# left the block cleanly → commit. The writes are now durable.
```

**3 — Irreversible effects gate.** Declare `reversible=False`. Add a `compensator` (a semantic inverse) if one exists; otherwise the effect blocks at commit until explicitly approved.

```python
from pherix import HTTPAdapter, GateBlocked

@tool(resource="http", reversible=False, injects_handle=False)
def refund_charge(customer, amount):           # the semantic inverse
    stripe.refund(customer, amount)

@tool(resource="http", reversible=False, injects_handle=False,
      compensator="refund_charge")
def charge_card(customer, amount):             # auto-commits; refunded on rollback
    return stripe.charge(customer, amount)

@tool(resource="http", reversible=False, injects_handle=False)
def send_email(to, body):                      # no inverse — an email can't be un-sent
    stripe.email(to, body)

adapters = {"sql": SQLiteAdapter(conn), "http": HTTPAdapter()}
with agent_txn(adapters, audit=audit) as txn:
    charge_card(customer="alice", amount=4200)
    receipt = send_email(to="alice@example.com", body="receipt")
    # send_email has no compensator → commit BLOCKS at the gate until a human
    # (or a higher-trust policy) approves the un-undoable effect:
    txn.approve_irreversible(receipt.effect_id)
# no approval → GateBlocked is raised and the staged effects never fire.
```

Reversible effects run live and roll back via the backend's own savepoints. Irreversible ones are *staged* — they only actually happen at commit, and an un-compensable one gates on explicit approval. Calling `rollback()` before commit means the irreversible effect simply never happened. That's the whole point.

## What you get

- **Undo the reversible** — DB and file writes roll back via the backend's own savepoints.
- **Gate the irreversible** — un-undoable effects stage and block at commit until approved.
- **Audit everything** — every effect, its arguments, and its outcome lands in the journal; the journal *is* the audit log.

## See it / explore

```bash
python -m examples.demo      # offline demo: an agent wipes a customers table and
                             # tries a large wire transfer — Pherix contains both
python -m pherix.inspector   # open the read-only audit console over the journal
```

Both run fully offline — no API key, no model.

## Learn more

- [`site/docs.html`](site/docs.html) — how it works, end to end.
- The rest of the static site (`site/index.html`, `site/get-started.html`, `site/demos.html`) is served by `python -m http.server` from the repo root.

## Status

Pre-release `0.0.0`, install from source. The engine is built; Pherix is **library-first** — a Python library plus a read-only audit console. No SaaS, no hosted service, no console-as-a-service. Source: [github.com/LukeyP02/Pherix](https://github.com/LukeyP02/Pherix).
