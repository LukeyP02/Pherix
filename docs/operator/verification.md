# Why you can trust the kernel — the invariants we machine-check

Pherix's moat is *verifiable correctness*: the layer between your agent and prod
is small enough to verify and proven enough to rely on. This page is the
evidence. It lists the **laws of the kernel** — properties that must hold for
*every* input, not just the scenarios we happened to imagine — and points at the
suite that checks each one over thousands of randomly generated cases.

These are **laws, not examples.** A normal test pins one scenario: "charge then
refund leaves balance 0". A law asserts the *algebra*: "`refund ∘ charge` is the
identity for **any** sequence of charges". The difference is what catches the
bug nobody hand-wrote — exactly the class a real agent's audit dogfood found
that 367 example tests missed.

## How the laws are checked

Property-based testing with [Hypothesis](https://hypothesis.readthedocs.io):
each law is a predicate over a *generated program* (a random sequence of tool
calls / crash points / concurrent schedules). Hypothesis searches the input
space for a counterexample and, on failure, shrinks it to the minimal one. The
suites live in `tests/test_laws_*.py`. Hypothesis is a **test-only**
dependency (`pip install pherix[test]`); the kernel itself imports nothing from
it and the whole suite runs offline.

> The bar is **depth of guarantee, not volume.** One property holding over all
> generated sequences beats a thousand pinned cases. We deliberately do *not*
> chase a high test-to-code ratio.

## The laws

### 1. The reversible fold is an involution — `tests/test_laws_reversible.py`

The journal is an append-only sequence of effects; commit folds it forward
(`apply` each), rollback folds it backward (`restore` each). Over any generated
program against the **real** SQLite savepoint machinery:

- **`rollback ≈ identity`** — folding forward then backward leaves the world
  byte-identical to the committed baseline. Every applied effect lands
  `COMPENSATED`, never half-undone.
- **commit is the forward fold** — a clean commit lands the *whole* program;
  all-or-nothing.
- **a mid-program exception unwinds the entire transaction** — because snapshot
  precedes apply, even the failing effect's partial write is restored; the world
  never shows a partial state.

### 2. A denied effect never touches a resource — `tests/test_laws_policy.py`

Policy is evaluated at stage-time, *before* the adapter applies a reversible
effect. Over arbitrary programs, for both a predicate `Deny` and a count `Cap`:

- a denial aborts the whole transaction and rolls it back to the committed
  baseline — **no partial application**, not even of the prior effects.

### 3. Compensators are true left-inverses — `tests/test_laws_compensator.py`

For irreversible effects there is no snapshot; the undo is a *semantic inverse*.
Over any sequence of charges fired then unwound by a mid-commit failure:

- **`compensator ∘ tool ≈ identity`** — the external world returns to baseline
  (under the catalog's semantic equality: a zero balance equals an absent
  account).
- **exactly-once** — each fired effect is compensated exactly once; the
  per-account arithmetic nets to zero (no double-refund, no skipped refund).

### 4. Crash recovery is terminal and exactly-once — `tests/test_laws_crash.py`

A crash can strike at any point of the fold; the durable journal is all that
survives, and `recover()` resumes from it. Fuzzing the crash point across the
whole fold:

- **terminal landing** — every recoverable transaction ends `ROLLED_BACK` (all
  standing effects undone) or `STUCK` (an irreversible with no compensator).
- **exactly-once** — each `APPLIED` irreversible is compensated once; a *second*
  `recover()` fires zero further compensators. The durable `status` is the
  idempotency fence.
- **the fence is honoured** — an already-`COMPENSATED` effect is never
  re-compensated; a `STAGED`/`FAILED` effect put nothing in the world and is
  never compensated.

### 5. Isolation has no false conflicts and no lost updates — `tests/test_laws_concurrency.py`

A concurrent schedule is captured by the version state the shared resource ends
in. Over random schedules:

- **no false conflict** — a transaction's own writes never conflict with
  themselves; disjoint key-sets never conflict. *This is the exact bug class the
  audit dogfood caught* — a read-then-write on one key spuriously conflicting on
  its own bump — now re-findable automatically if reintroduced.
- **no lost update** — if a foreign transaction committed a write to a key this
  one read, the commit-time diff **always** flags it (first-committer-wins).

### 6. Backends are differentially equivalent — `tests/test_laws_differential.py`

The same effect sequence folded through two reversible adapters must yield the
same committed world, the same restored world, and the same per-effect status
sequence — the *backend* varies, the journal algebra does not. Checked today
between the real `SQLiteAdapter` and an in-memory reference oracle; a Postgres
variant is wired and skips cleanly until that adapter lands.

### 7. Adversarial input fails loud and safe — `tests/test_laws_adversarial.py`

The one outcome we forbid is *silently wrong*. Over hostile / malformed input:

- a **non-journal-able arg** raises `EffectArgsError` at the idempotency
  boundary, before anything is journalled or applied; the world is untouched.
- **SQL-injection payloads** are inert under parameterised SQL — stored
  verbatim, no table dropped or created.
- **path-traversal** strings never escape the filesystem root.
- **oversized payloads** (multi-MiB) round-trip losslessly through commit and
  the audit journal.
- a **corrupted / unknown-status** durable journal makes `recover()` fail loud;
  a standing irreversible whose tool is gone lands `STUCK` (fail-safe), never a
  silent rollback that would falsely imply the side effect was undone.

## What is *not* claimed

Honesty is the product. These laws cover the **kernel** (`pherix/core/`): the
journal fold, recovery, isolation, policy, compensation. They do **not** cover:

- per-adapter exhaustiveness — each adapter worktree owns its own golden /
  failure tests; this suite tests the engine, not every backend's edge cases.
- cross-**host** isolation — single-host (in-process + cross-process on a shared
  SQLite file) is what's verified; cross-host arbitration is the control plane's
  job.
- anything Pherix explicitly cedes (durable *code* execution, observability,
  agent orchestration).
