# Parity stream (T3) — pressure log

The SDK-level Py<->TS parity suite (`tests/test_sdk_parity.py` +
`pherix-ts/test/parity/runner.mts`) drives both engines on the same scenario and
diffs the journals. Entries below record where the frozen engine resisted, or
where the two SDKs diverge in a way the suite cannot fix from outside the engine.

---

### Effect.result is not journal-serialisable uniformly across the two SDKs
- **Doing:** Express the isolation-conflict scenario identically in both SDKs.
  The natural read tool returns whatever the isolation helper returns.
- **Resisted:** Python's `execute_isolated` returns a raw `sqlite3.Cursor`,
  which the Python audit journal cannot serialise (`strict_json_default` raises
  on `Cursor`) — the txn blew up during `update_effect`, not at my assertion.
  TS's `executeIsolated` returns rows (`.all()`) or run-info, both
  JSON-serialisable. So the *same* tool body is safe in TS but unsafe in Python.
- **Smallest fix:** Not an engine change — the parity scenario returns a
  serialisable row (`{"balance": ...}`) on both sides, sidestepping it. But the
  underlying asymmetry is real: `execute_isolated` (Python) hands back a live
  cursor whose lifetime is bound to the connection, while `executeIsolated` (TS)
  eagerly materialises. If a future generalisation wants the two helpers to be
  true mirrors, Python's should materialise (`.fetchall()` for readers) so a
  tool that simply `return`s the helper's result is journal-safe in both
  languages. Logged for A7; the helper lives in the adapter file (off-limits to
  T3), so not patched here.

### readsCommittedOnly() honesty differs by language (on-disk only)
- **Doing:** Keep the isolation-conflict scenario on `:memory:` so both SDKs
  take the same conflict-diff branch.
- **Resisted:** Nothing for the in-memory case — both agree
  (`readsCommittedOnly() == false`, the own-write-visible branch). But TS's
  `SqliteAdapter.readsCommittedOnly()` is HARDCODED `false` with a comment that
  the committed-only (cross-process meta-connection) path is deferred, whereas
  Python returns `true` for an on-disk DB (it opens a separate autocommit meta
  connection). So an *on-disk* isolation scenario would diverge: Python compares
  against the committed base, TS against `last_my_write`. The suite cannot cover
  an on-disk conflict scenario as true parity until TS gains the meta-connection
  path.
- **Smallest fix:** Bring TS's `SqliteAdapter` to honest committed-only reads
  on-disk (a second connection, as Python does), OR have both SDKs agree to the
  in-process tier only and document on-disk cross-process as out-of-scope for
  both. Either way it is an adapter-file change (T1/A7 territory), not engine,
  and not a blocker for the in-memory core scenarios. Logged for the harvest.

### No engine resistance for the three core scenarios
- **Doing:** reversible-commit, irreversible-gate, isolation-conflict, each
  through `agent_txn` / `agentTxn` unchanged.
- **Resisted:** Nothing. `TxnState` and `EffectStatus` share identical wire
  strings across both languages, so the canonical journal compares directly with
  no language-specific enum mapping — strong evidence the substrate is already
  one system at the journal level. The only fields needing normalisation
  (`txn_id`, `effect_id`, `ts`, `result`, `snapshot`, `args`) are exactly the
  ones that *should* differ per-run or per-driver; none are structural.
- **Smallest fix:** None needed — this is the convergence the design predicts.
