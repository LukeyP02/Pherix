# A7 — engine harvest decision record

A7 reads every stream's pressure log and either makes **one coherent
generalisation + slim** of both engines, or **declines with a one-line reason**
(per `README.md`). The bar: engine LOC flat-or-down while adapter coverage is up;
bulk-adding generalisations are the failure mode and get rejected.

**Coverage delta this worktree:** TS adapters 4 → 14 (`+git, s3, redis, mongodb,
mysql, dynamodb, gcs, elasticsearch, rest, messagequeue`); SDK parity suite 0 → 13
scenarios driving both engines. **Engine LOC: unchanged** (Python 1119, TS 998
across `{transaction,effects,runtime,tools}`). Coverage up, engine flat — the bar
is met by *not touching* the substrate, which is the result the design predicts
when the abstraction holds.

---

## The verdict: decline. The substrate passed the convergent-generalisation test.

Ten new adapters satisfied the `ResourceAdapter` protocol **unchanged**, and the
13-scenario parity suite confirms both engines produce structurally identical
journals with no language-specific mapping. Per `CLAUDE.md`: *"A new adapter
satisfying the protocol unchanged confirms the abstraction."* That is exactly what
happened. There is no recorded pressure that a generalisation would lift — every
note is a confirmation, a self-declared premature trend, or an adapter-file
follow-up. A7 makes no engine change. Each note below is dispositioned.

### Declined — the async-construction lifecycle hook (`ts-adapters-a.md` #1)
- **The pressure:** MySQL (TS) can't run its eager `_pherix_versions` DDL in the
  constructor because the driver is async-only — the *second* adapter after
  Postgres to hit the sync-Python / async-TS construction gap. Proposed general
  form: an optional async `init()` lifecycle hook on the protocol.
- **Declined because:** two occurrences, each cleanly handled by a once-guarded
  lazy `ensureVersionsTable()` that is **observably identical** to a caller. The
  stream author themselves judged it *"not worth adding for two — flagging the
  trend."* Adding a protocol lifecycle method now is the textbook bulk-adding
  generalisation the discipline rejects: it would touch the frozen `base.*` +
  every adapter's begin path to lift zero current pressure. **Revisit at the
  third occurrence** — a third async-driver adapter needing eager setup is the
  confirmed-abstraction signal that flips this from decline to generalise.

### Confirmed, no change — sync/async `apply` uniformity (`ts-adapters-a.md` #2, `ts-adapters-b.md` #3)
- Both adapter streams independently verified that the runtime already wraps
  `adapter.apply` in `async () => …` and awaits it (`runtime.ts:233` and `:326`),
  so a synchronous throw and a rejected promise propagate identically. This is the
  existing awaitable-lane abstraction in `base.ts` *working as designed* — a
  confirmation of the substrate, not pressure against it. The lesson the notes
  draw (pin the partial-failure conformance property to the async tool shape so an
  author doesn't write a sync-throw test that bypasses the promise path) is a
  **test-harness convention, not an engine defect** — out of A7's engine scope and
  correctly handled in the adapter test files (all 210 TS tests green at HEAD).

### Confirmed intentional, no change — versioned-adapter detection asymmetry (`ts-adapters-b.md` #2)
- Python's irreversible adapters carry `read_version`/`write_version` stubs that
  *raise*; TS omits them and the TS isolation layer detects versioned adapters
  **structurally** (by method presence). Both correctly exclude irreversible
  effects from the isolation diff, by different idioms. This lives in
  `isolation.{py,ts}`, which is **outside A7's engine files** and is not pressure
  on `{transaction,effects,runtime,tools}`. Recorded as deliberate and
  parity-safe; no engine action.

### Surfaced as adapter-axis follow-ups — NOT folded into this worktree (`parity.md` #1, #2)
Two real Py↔TS divergences live in `adapters/sql.{py,ts}`, not the engine. They are
not blockers (the parity suite covers them on the agreeing `:memory:` branch and
sidesteps the cursor case in-scenario), and fixing them is **scope creep against
A7's engine mandate** — each needs its own considered change with test updates, so
they are surfaced for a future adapters pass rather than smuggled in here:

1. **`execute_isolated` return-type asymmetry.** Python returns a live
   `sqlite3.Cursor` (not JSON-serialisable — a tool that simply `return`s it
   crashes the audit journal's effect-result serialisation); TS's `executeIsolated`
   eagerly materialises rows. This is also a *latent Python correctness bug*
   independent of parity. The fix (materialise via `.fetchall()` for readers) is
   **not a safe one-liner** — existing Python isolation tests call `.fetchone()` on
   the returned cursor, so it ripples into those call sites and must land with its
   test updates. Deferred deliberately, not forgotten.
2. **`readsCommittedOnly()` on-disk honesty.** TS `SqliteAdapter` hardcodes
   `false` (committed-only meta-connection path deferred); Python returns `true`
   on-disk via a separate autocommit connection. Agree on `:memory:`; an *on-disk*
   isolation scenario would diverge until TS gains the meta-connection path.

---

**Net:** engines untouched and verified general by 10 zero-resistance ports +
13 green parity scenarios; one generalisation declined as premature (revisit at
n=3); two adapter-file divergences surfaced as scoped follow-ups. Engine LOC
flat — the correct outcome when the substrate already generalises.
