# Pherix — Claude Code Guide

> Read this fully before writing any code. It is the whole system.

## What Pherix is

**A transactional resource runtime for AI agents.** It gives ACID-style guarantees —
atomicity, isolation, capability enforcement, durability — over the *external
side-effects* of agent tool calls (database writes, file writes, API calls).

An agent today calls a tool and the effect just *happens*: irreversibly,
immediately, unaudited, with no isolation from other agents. Pherix sits at the
tool-call layer and makes those effects behave like database transactions.

**Pherix is a library.** You `pip install` it, wrap your agent's tool-call layer,
and keep your existing agent loop and model provider. It is front-end-agnostic —
later a proxy/MCP gateway front-end wraps the same core.

### What Pherix is NOT — read this so you don't drift

- **Not durable execution.** Temporal replays your *code* to survive crashes.
  Pherix transacts your *resources*. If a feature looks like "make the agent's
  code resumable after a crash / retry orchestration / workflow engine" — it is
  **out of scope**. That is Temporal's turf and we cede it deliberately.
- **Not observability.** LangSmith / Langfuse / Arize *watch* and log traces.
  Pherix *enforces*. Observability falls out for free (we hold the journal
  anyway) but it is a byproduct, not the product.
- **Not an agent framework.** Pherix does not run agents, call LLMs, or
  orchestrate prompts. It wraps the tool-call layer of an agent that already
  exists.

### The competitive wedge — what only Pherix does

1. **Resource-state snapshot/rollback via real backend semantics** — DB
   savepoints, filesystem copy-on-write. Nobody does this.
2. **Capability/policy enforcement** as a first-class commit-time primitive.
3. **MVCC-style isolation** between concurrent agents.

---

## The core insight — ONE solution for all five hard problems

There are five hard problems (snapshotting, isolation, saga/partial-failure
correctness, deterministic replay, speculative dry-run). They are **not five
subsystems**. They collapse onto a single engine:

> **The versioned effect journal, mediated by resource adapters.**

Two primitives, one idea:

- **The journal** — every side-effecting tool call becomes an `Effect`: an entry
  in an append-only, ordered, versioned log held by a `Transaction`.
- **The adapter** — a `ResourceAdapter` makes journal entries executable and
  reversible against a *class* of real resource (`snapshot → apply → restore`).

Every capability is then just a **traversal of the journal**:

| Capability | Journal operation |
|---|---|
| Commit | fold **forward**, `adapter.apply` each effect |
| Rollback | fold **backward**, `adapter.restore` / run compensator |
| Replay | fold **forward** against fresh state, assert identical results |
| Dry-run | fold **forward** against a snapshot, then discard |
| Isolation / conflict detection | **diff** this txn's read/write keys + versions against the journal |
| Audit / observability | the journal **is** the audit log — just read it |

Five problems → one engine + one protocol. **Slice 1 builds that engine.** Every
later slice is a thin feature that traverses the journal differently.

**Honest caveat:** one elegant *core* is not the same as less *work*. You still
write each adapter, each compensator, and the conflict-resolution policy. But you
build the engine exactly once.

---

## Execution model

- **Reversible effects** (including reads) → execute **live**, journalled.
  `rollback()` restores the before-snapshot via the adapter.
- **Irreversible effects** → **staged**, the agent gets a `StagedResult`
  placeholder. They fire at `commit()`:
  - has a registered compensator → auto-commits, compensator used on rollback
  - no compensator → **gates**: `commit()` blocks, requires explicit
    `approve_irreversible()` (a human, or a higher-trust policy).
- **Policy** is evaluated **twice** — at stage-time (fail fast) and again at
  commit-time (state may have changed between the two — TOCTOU safety).

`commit()` is the moment irreversible effects actually happen. `rollback()`
before commit means irreversible effects simply never happened — that is the
entire point.

---

## Architecture

```
pherix/
  core/
    transaction.py   Transaction: state machine + the ordered effect list (the journal)
    effects.py       Effect: one journalled tool call
    adapters/
      base.py        ResourceAdapter protocol
      sql.py         SQLiteAdapter / PostgresAdapter (savepoints)
      filesystem.py  FilesystemAdapter (copy-on-write)        [Slice 2]
      http.py        HTTPAdapter (irreversible, no rollback)  [Slice 3]
    tools.py         @tool decorator + registry
    policy.py        capability policy — stage-time + commit-time eval
    isolation.py     read/write-set tracking, conflict detection  [Slice 4]
    audit.py         append-only journal persistence (SQLite), replay support
    runtime.py       agent_txn() context manager — the orchestration
  frontends/
    library.py       the Python API surface
    proxy/           MCP gateway front-end                    [Slice 8]
tests/
```

**Discipline:** `frontends/` is thin; `core/` knows nothing about how it is
driven. That is what lets the MCP proxy bolt on later with no rewrite.

---

## Key data structures

These are the shapes. Exact field names can evolve; the structure should not.

```python
class EffectStatus(Enum):
    STAGED, APPLIED, COMPENSATED, GATED, FAILED

@dataclass
class Effect:
    effect_id: str            # idempotency key = hash(txn_id + index + tool + sorted args)
    txn_id: str
    index: int                # ordering within the transaction
    tool: str
    args: dict
    resource: str             # which adapter handles this
    reversible: bool
    read_keys: list[tuple]    # (resource, key, version) read — for isolation [Slice 4]
    write_keys: list[tuple]   # (resource, key) intended writes  — for isolation [Slice 4]
    status: EffectStatus
    snapshot: SnapshotHandle | None
    result: object | None
    compensator: str | None   # name of registered compensating tool, if any
    ts: datetime

class TxnState(Enum):
    OPEN, STAGED, COMMITTED, ROLLED_BACK, PARTIAL, STUCK

@dataclass
class Transaction:
    txn_id: str
    state: TxnState
    effects: list[Effect]     # THE JOURNAL — append-only, ordered
    policy: Policy
```

`Effect` carries `read_keys` / `write_keys` from day one even though isolation is
Slice 4 — so isolation is never a retrofit.

---

## The ResourceAdapter protocol

The abstraction that makes Pherix a system and not a logging decorator.

```python
class ResourceAdapter(Protocol):
    name: str
    def supports_rollback(self) -> bool          # honesty flag
    def snapshot(self, effect: Effect) -> SnapshotHandle
    def apply(self, effect: Effect) -> object    # execute the effect
    def restore(self, handle: SnapshotHandle) -> None
```

- **`SQLAdapter`** (SQLite + Postgres): `snapshot()` issues a real `SAVEPOINT`,
  `restore()` does `ROLLBACK TO SAVEPOINT`. The database does the heavy lifting —
  correct by construction. `supports_rollback()` → `True`.
- **`FilesystemAdapter`** [Slice 2]: copy-on-write / content-hash backup of
  touched paths.
- **`HTTPAdapter`** [Slice 3]: `supports_rollback()` → `False`. It *cannot*
  snapshot — so the runtime forces the effect down the irreversible path
  (stage + gate/compensate). Pherix is **honest about what it cannot undo**
  rather than pretending.

Tools declare which resource(s) they touch; the runtime routes each effect to the
right adapter.

---

## Build slices

Each slice ships something demoable. The system gets *deeper*, not just wider.

| Slice | Delivers |
|---|---|
| **1** | SQL adapter (savepoints) + `Transaction` state machine + `agent_txn()` + SQLite journal + allow-list policy. Reversible path end-to-end. |
| 2 | Filesystem adapter (copy-on-write) — proves the adapter protocol is a real abstraction. |
| 3 | HTTP/irreversible adapter — staging, `StagedResult`, gate, compensation registry, idempotency keys. |
| 4 | Isolation — read/write-set tracking, conflict detection, resolution policy (abort/retry/serialize). |
| 5 | Replay from the journal. Nearly free once the journal exists. |
| 6 | Real policy engine — capability grants, spend caps, content-aware rules, commit-time re-eval. |
| 7 | Speculative dry-run diff. Nearly free once the journal exists. |
| 8 | MCP gateway front-end on the same core. |

---

## SLICE 1 — what to build now

Build in this order. **Tests first** for the core logic (it is pure, well-understood
behaviour — TDD applies).

1. **`pyproject.toml` is done; repo skeleton is done.** Confirm `pytest` runs.
2. **`core/effects.py`** — `Effect` dataclass + `EffectStatus` enum. Include
   `read_keys` / `write_keys` slots now (no retrofit later).
3. **`core/transaction.py`** — `Transaction` + `TxnState` state machine, ordered
   effect list, `txn_id` generation.
4. **`core/adapters/base.py`** — the `ResourceAdapter` protocol + `SnapshotHandle`.
5. **`core/adapters/sql.py`** — `SQLiteAdapter`: `snapshot()` → `SAVEPOINT`,
   `apply()` runs the effect, `restore()` → `ROLLBACK TO SAVEPOINT`,
   `supports_rollback()` → `True`. (Postgres variant is the same shape — can stub.)
6. **`core/tools.py`** — `@tool` decorator + registry. A tool declares its
   `resource` binding and `reversible` flag. Unregistered tools are invisible to
   the runtime.
7. **`core/audit.py`** — SQLite append-only journal persistence: transaction +
   effect records, snapshots stored as JSON.
8. **`core/policy.py`** — minimal allow/deny list, evaluated at stage-time. The
   real engine is Slice 6.
9. **`core/runtime.py`** — `agent_txn()` context manager: intercept registered
   tool calls → build `Effect` → route to adapter → `snapshot` → `apply`
   (reversible path) → journal. `commit()` finalises the journal; `rollback()`
   folds the journal backward calling `adapter.restore()`.
10. **`frontends/library.py`** — expose `agent_txn`, `@tool`, `approve_irreversible`.

### Slice 1 is done when

You can wrap an agent loop with a SQLite-writing tool, watch reversible writes
journal live, call `rollback()` and see the row gone, call `commit()` and see the
row persist — and the audit journal shows the entire story. Plus:

- `pytest -q` → 0 failures
- tests cover: savepoint snapshot/restore correctness, `Transaction` state
  transitions, rollback actually restoring DB state, journal completeness,
  policy denial at stage-time

---

## How to explain things to the operator

The operator on this project is a **maths-background thinker, not a CS-theory one.**
Their formal reasoning is strong; their fluency in programming jargon, framework
conventions, and CS folklore is lighter. They want to *learn the mental model* as we
build, not rubber-stamp decisions. This applies to every agent on the project —
orchestrator, workers, reviewers. Adjust accordingly:

- **Define jargon inline the first time you use it.** "Protocol — Python's term for
  an interface contract." "ContextVar — like thread-local state but safe for async."
  Don't lean on the term and expect recognition.
- **Lead with the logical chain, not the precedent.** Say *why* something follows
  ("X holds because Y and Z compose this way"), not "this is just how it's done."
  Logical structure they trust; appeals to convention they don't.
- **Use analogies that bottom out in first principles.** The journal is an
  append-only sequence; commit and rollback are forward and backward *folds* over
  it; an adapter is a triple `(snapshot, apply, restore)` over a resource; ACID
  isolation is about *composition* of concurrent operations. Maths analogies
  (composition, ordering, inverses, state machines) land better than software war
  stories.
- **Trust the operator's reasoning.** If you find yourself simplifying past the
  actual decision, you've gone too far — the goal is *clarity*, not infantilisation.
  Same depth, better signposting.
- **Planning is the exception that earns length.** Q&A: short. Exploratory or
  "explain how X works": full picture with terms defined and trade-offs surfaced.
  Match length to the operator's question, not to your own thoroughness instinct.

---

## Conventions

- **Python 3.12.** snake_case functions/vars, PascalCase classes.
- **Dataclasses** for internal structures (no API layer yet, so no Pydantic
  needed until a front-end needs wire types).
- **Parameterised SQL always** — never string-interpolate into queries.
- **Tests:** `backend`-style layout under `tests/`, `test_<module>.py`. Pherix
  does **not** call LLMs itself — it wraps tools — so the whole suite runs
  offline with no API key. Keep it that way.
- **No over-engineering.** Minimum complexity for the current slice. No
  backwards-compat shims for code that was simply deleted. Do not build Slice N+1
  abstractions while doing Slice N.
- **Commit hygiene:** conventional-commit prefixes (`feat:`, `fix:`, `test:`,
  `refactor:`). Small, vertical commits.
- `git push` requires explicit human instruction — never push autonomously.

---

## If you are unsure

The single most important mental model: **everything is a traversal of the
journal.** If you are about to write a feature and it does not reduce to "fold the
effect journal forward / backward / against a snapshot, or diff it" — stop, and
re-check it against this document. It almost certainly does reduce to that.
