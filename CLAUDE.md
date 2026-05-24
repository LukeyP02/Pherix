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

Five problems → one engine + one protocol. **The engine is built** (Phase 1: all
lanes + isolation + replay + policy + dry-run + the MCP gateway + the
hardening pass). Every feature on top is a thin traversal of the journal — and the
canonical framing for *what* those features are is the **four axes** below.

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

## The four axes — the canonical framing

The journal-traversal above is the *substrate*. On top of it, Pherix is exactly the
answer to the four questions any system must answer to make an action transactional —
**one axis each, extended independently**, and *complete*: there is no fifth question,
and everything else collapses into these four + the journal.

| Axis | Question it answers | Filled by |
|---|---|---|
| **Interception** | How do actions reach us? | the Python library, the **TypeScript SDK**, the MCP gateway, the agent-agnostic sandbox |
| **Policy** | What do we allow? | allow/deny + caps + the human gate + world-state-aware rules — a clean slate the buyer writes against |
| **Adapters** | How do we undo what *can* be undone? | per-resource `snapshot/apply/restore` (the backend does the undo) |
| **Compensators** | How do we undo what *can't*? | the staged/gated lane + a catalog of vetted semantic inverses |

**Adapters and compensators are two *different* undo mechanisms, not one.** Adapter
restore = *state rollback* (snapshot, then revert — exact, needs no knowledge of what
the action meant). Compensator = *semantic inverse* (run an opposite action —
approximate, needs the meaning). Restore does **not** "use compensation"; compensation
is the fallback for exactly when snapshotting is impossible. The boundary is
`supports_rollback()`. They meet only at the journal — the backward fold calls one or
the other per effect.

**Everything else is a consequence, not an axis:** audit = the journal, read; isolation
= a correctness property of adapters + journal under concurrency; replay / dry-run =
operations over the journal; the control plane = how the four axes are delivered and
sold at org scale; governed memory = an adapter + a policy pointed at the agent's
memory.

**Convergent generalisation — the architectural test.** As we grow an axis, the shared
substrate must get *more general, not more bloated*. A new adapter satisfying the
protocol unchanged confirms the abstraction; an extension that forces the fold to
generalise (crash-recovery driving the fold from the durable journal; cross-process
isolation; world-state policy) lifts *all four* axes at once. The failure mode to avoid
is **divergent special-casing** — per-feature engine branches. If a feature wants engine
surgery, that is the signal to generalise the substrate, not special-case it. A core
that generalises as it grows is why "small" is correct, not thin.

**Two languages, no more.** Most agent infra is Python or TypeScript. We do both and
nothing else — no Go/Java SDKs. The MCP gateway + the sandbox cover other-language
agents without a per-language SDK.

---

## Status & what to build now

**Phase 1 — the engine — is done.** Both lanes, MVCC isolation, replay, policy,
dry-run, the MCP gateway, plus the engine-hardening pass that made the moat claims
true: crash-consistent recovery (`core/recovery.py`), cross-process isolation
(single-host), world-state policy (`PolicyContext.read`), the longitudinal envelope
(`core/envelope.py`). **1011 Python tests + 210 TypeScript, fully offline.**

**Each axis is now at base in *both* languages** — the "flesh each axis to its base,
in two languages" push has landed:

- **Adapters** — *done.* Python carries 16 adapters (SQLite, Postgres, MySQL,
  MongoDB, Redis, S3, GCS, DynamoDB, Elasticsearch, filesystem, HTTP, REST, MQ, git,
  memory, …); TypeScript mirrors **14** at semantic + field-name parity (`pherix-ts/src/adapters/`).
  Backend drivers stay optional, lazily imported; the kernel is dependency-free.
- **Compensators** — *done.* Vetted left-inverse catalog at module parity in both
  languages (`{identity,payments,provisioning,saas}`), each tested through the
  partial-failure path.
- **Interception** — *TypeScript SDK at parity.* `tests/test_sdk_parity.py` drives the
  same scenario through both engines and asserts **structurally identical journals**
  (13 scenarios) — the proof the two SDKs are one system. The substrate passed the
  convergent-generalisation test: 10 new TS adapters fit `ResourceAdapter` *unchanged*,
  so the engine LOC is flat while coverage is up.
- **Policy**: starter templates built; expressiveness is the remaining edge.

**Two open adapter-axis follow-ups** (surfaced by the parity suite, deliberately
scoped out of the engine pass): `sql.py`'s `execute_isolated` returns a live
`sqlite3.Cursor` that isn't journal-serialisable (a latent Python correctness bug —
materialise via `.fetchall()` for readers); and TS `SqliteAdapter.readsCommittedOnly()`
hardcodes `false`, so on-disk cross-process isolation parity awaits the TS
meta-connection path. Parity is proven for `:memory:` today.

Build to the **base** (the common things any buyer assumes work); let the design
partner bring the **edge** cases. Test each axis *hard* — golden / failure / crash /
adversarial paths, each new claim shipping a test that fails against the prior commit
— but not ratio-chasing (SQLite's ~600:1 is the anti-target). Then the demos, then
the first design partner; the control plane (cross-host / hard cross-process arbiter,
audit search, the anonymised policy library) is pulled by that partner.

---

## How to explain things to the operator

The operator on this project is a **maths-background thinker, not a CS-theory one.**
Their formal reasoning is strong; their fluency in programming jargon, framework
conventions, and CS folklore is lighter. They want to *learn the mental model* as we
build, not rubber-stamp decisions. This applies to every agent on the project —
orchestrator, workers, reviewers. Adjust accordingly:

- **Default register is maths or physics, not software jargon.** Lead with the
  algebraic or dynamical framing, not the framework convention. The journal is an
  *append-only sequence* / *time series*; commit and rollback are *forward and
  backward folds* over it; an adapter is a triple `(snapshot, apply, restore)` over
  a resource; a compensator is a *semantic left-inverse* (`compensator ∘ tool ≈
  identity`); ACID isolation is about *non-commutativity* of concurrent operations;
  TOCTOU is the problem that `world_state` evolves between two evaluations of the
  same predicate `P(effect, world_state)`; an effect is an *observable*, a
  transaction is a *measurement* with a defined boundary in time; the journal's
  read/write keys form a *partial order* under happens-before. Reach for maths /
  physics first; reach for software war stories not at all.
- **Define CS jargon inline the first time you use it.** "Protocol — Python's term
  for an interface contract." "ContextVar — like thread-local state but safe for
  async." Maths and physics terms are fair game if used precisely; CS-specific
  jargon needs defining.
- **Anchor CS abstractions on the problem they solve, *before* naming them.**
  Defining the term inline is necessary but not sufficient. When introducing a
  decision framed around an algorithmic concept the operator isn't already
  holding ("verification vs recovery replay", "equality semantics",
  "fresh-state factory", "consistent hashing", "two-phase commit"), lead with
  the *failure mode* or the *question the operator would naturally ask in
  their own words*, not the abstraction's name. The shape: concrete scenario
  → plain-English options → only then the formal terms (if even useful by
  then). The operator's maths intuition recognises the abstraction on its own
  once anchored to a concrete problem. Abstraction-first lists of D1–D5
  decisions read as jargon even when each individual term has been defined —
  the operator's note that "it's all going above my head" is the signal that
  this rule was broken. When in doubt, ask "what would the operator type into
  Google to find out about this?" — that phrasing is the right hook for the
  explanation.
- **Lead with the logical chain, not the precedent.** Say *why* something follows
  ("X holds because Y and Z compose this way"), not "this is just how it's done."
  Logical structure they trust; appeals to convention they don't.
- **Default Luke-facing output is HTML, not markdown.** Anything the operator will
  *re-read or share* — decision aids, architecture explainers, walked-through
  examples, slice deep-dives — ships as a self-contained HTML artifact under
  `docs/`, served by the single `python -m http.server` at the repo root. Markdown
  stays the register for *agent-consumed* artifacts (`CLAUDE.md`, `TASK.md`,
  `ROADMAP.md`) and brief chat answers. The global `~/.claude/CLAUDE.md` §"Output
  format" defines the bar HTML must clear; this project enforces it.
- **Trust the operator's reasoning.** If you find yourself simplifying past the
  actual decision, you've gone too far — the goal is *clarity*, not infantilisation.
  Same depth, better signposting.
- **Planning is the exception that earns length.** Q&A: short. Exploratory or
  "explain how X works": full picture with terms defined and trade-offs surfaced.
  Match length to the operator's question, not to your own thoroughness instinct.
- **Keep Luke-facing HTML current with git state.** Every slice merge and every
  big architectural decision triggers a sweep of the `docs/` suite as part of
  the same change: update status colours (done / active / pending) and "you are
  here" markers in `docs/index.html` and `docs/roadmap.html`; refresh commit
  SHAs and test counts; bring slice cards' status pills and body copy in line
  with `docs/ROADMAP.md`; resolve / strike-through any follow-ups that the
  merge addressed. Retire decision-aid pages once their slice merges
  (`docs/slice-N-decisions.html` — delete the file and remove its hub card; the
  reasoning lives in the code, the merge commit, and `docs/ROADMAP.md`). The
  Slice 8+ trilogy — **determinism, memory, audit** — must stay consistently
  reflected across `future.html`, `roadmap.html`, and the hub. Stale HTML is
  technical debt; flag and fix as part of every merge sweep, not later.

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
