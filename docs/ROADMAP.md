# Pherix Roadmap

Slice-by-slice build status. The full spec for each slice lives in `CLAUDE.md`.

## Slices

- [x] **Slice 1** — SQL adapter (savepoints) + `Transaction` state machine + `agent_txn()` + SQLite audit journal + allow-list policy. Reversible path end-to-end. *(merged `ad4e9c0`)*
- [x] **Slice 2** — Filesystem adapter (copy-on-write) + `TransactionalResourceAdapter` sub-protocol + mixed-resource transactions. *(merged `c926f97`)*
- [x] **Slice 3** — HTTP/irreversible adapter — staging, `StagedResult`, gate, compensator-as-tool, mixed-fold partial-commit unwind with `STUCK` for missing compensators. *(merged `a13018b`)*
- [x] **Slice 4** — Isolation — read/write-set tracking, commit-time conflict diff, resolution policy (`Abort` / `Retry(N)` / `Serialize`), in-process `JournalRegistry` and filesystem-shared SQLite cross-process arbitration via the `SQLiteAdapter` meta-connection. *(merged `0fab344`)*
- [ ] **Slice 5** — Replay from the journal.
- [ ] **Slice 6** — Real policy engine — capability grants, spend caps, content-aware rules, commit-time re-eval.
- [ ] **Slice 7** — Speculative dry-run diff.
- [ ] **Slice 8** — MCP gateway front-end.

## Follow-ups from Slice 1 review

Captured from the `feat/slice-1` review — decide before the slice noted.

- ~~**Non-serializable args silently `str()`-coerced**~~ Resolved before Slice 3: `effects.strict_json_default` supports `bytes` (base64), `datetime` (ISO 8601), and any `@dataclass` (recursive `asdict`); anything else raises `EffectArgsError` at `Effect` construction. `audit.py` shares the same serialiser. Idempotency keys are now collision-safe by construction across distinct non-trivial types.
- ~~**Adapter lifecycle is duck-typed, not in the Protocol.**~~ Resolved in Slice 2: `TransactionalResourceAdapter` sub-protocol introduced in `adapters/base.py`; `runtime.py` dispatches lifecycle via `isinstance` rather than `hasattr`. A typo'd `begin` now fails at type-check rather than silently skipping.
- **`_guard_thread` comment oversells coverage.** It catches explicit sharing of the `TxnContext` across threads (good, tested), but not the silent case — a tool dispatched to a worker thread where `active_txn` is empty, so the wrapper runs it raw. Mitigation is reasonable; tighten the comment so it doesn't imply full coverage. *(nit — anytime)*
- **Default `AuditJournal()` is `:memory:`.** Fine for tests/demo, but durability is a stated core property — a non-durable default journal is an odd default. Decide: on-disk default, or keep in-memory with explicit opt-in. *(before Slice 5 — replay needs a durable journal to replay from)*

## Follow-ups from Slice 3 review

- **Compensator contract is args-only, not result-piped.** `_partial_unwind` invokes `compensator(*effect.args)` — the original tool's *return value* (e.g. a real Stripe `charge_id`) is in the journal but isn't piped into the compensator. Today's mitigation: design tools to re-derive identity from args (use Pherix's `effect_id` as an upstream idempotency key, look up the original by that key inside the compensator). Proposed extension: opt-in `@tool(compensator='n', pipe_result=True)` form — compensator signature becomes `(result, **original_args)`, backwards-compatible default. *(before first real backend integration — the obvious Stripe wiring trips on this)*
- **Compensator execution isn't journalled as a separate row.** `_partial_unwind` constructs a synthetic `Effect(index=-1, ...)` to carry the compensator's fire through `adapter.apply`; the original effect's status flips `APPLIED → COMPENSATED` in place, but the compensator's own execution isn't an audit row. Auditability concern for Slice 8++ (the audit pillar): "when did the refund actually fire?" can't be answered from the journal alone. Decide: append-the-compensator-as-a-real-effect, or extend the row schema with a `compensator_ts` column, or accept the current compression. *(audit-pillar concern)*
- **Idempotency test is a pin, not a scenario.** Worker asserts "re-fire of an `APPLIED` effect is a no-op" by flipping status in-test, because no Slice 3 path re-enters commit. The property is load-bearing for Slice 5 replay; strengthen with a real replay scenario when replay arrives. *(strengthen in Slice 5)*

## Follow-ups from Slice 4

Captured during Slice 4 implementation; decide before Slice 5 lands or the noted slice.

- **Monotonic-counter MVCC cannot distinguish self-bumps from cross-txn writes.** When a txn reads then writes the same key, its own bump moves the version — `check_conflicts` filters those via an own-writes set, so a real lost-update where another txn *also* wrote the same key is treated as a self-bump and slips through. Slice 4's canonical lost-update pin therefore uses a read-only loser txn (first-committer-wins is unambiguous there). The fix is per-adapter: Postgres SSI or row-ctid, or carrying the original version in `write_keys` and checking move-since-our-write. *(close before Slice 5 leans on the journal for replay)*
- **Audit journal does not persist `read_keys` / `write_keys`.** The Slice 1 schema has `args / snapshot / result` but not the isolation key triples; they live only on the in-memory `Effect`. A post-mortem of an `IsolationConflict` from the audit journal alone is therefore lossy. The fix is a schema bump on `effects` (two TEXT columns) and a `_dump` pass at record-time. *(audit-pillar concern; close before Slice 8 gateway, which leans on the audit journal as the cross-host arbitration substrate)*
- **`SQLiteAdapter` leaks a meta-connection per construction.** The Slice 4 meta-connection (opened in `__init__` for cross-process read-version visibility) is closed only by GC. Add an explicit `close()` and call it from `agent_txn`'s teardown / a context-manager interface. *(housekeeping — anytime; trivial)*
- **Serialize cross-process is documented as Abort-degrade, not enforced.** The runtime's `_run_isolation_check` waits via the in-process `JournalRegistry` then runs the diff; cross-process in-flight writers are invisible to that registry. The behaviour is correct (degrades to Abort via the post-wait diff) but a deliberate "no cross-process Serialize" guard in `Serialize.__init__` would make the contract louder. *(decide alongside Slice 8 gateway scope)*
- **`_CONN_ADAPTERS` keyed by `id(conn)` accumulates.** One map entry per `SQLiteAdapter` construction, never freed. Negligible in test / agent-runtime contexts (O(handful) connections per process); becomes real if a long-lived process churns many SQLite connections. Add an explicit unregister or move to `WeakValueDictionary` via a wrapper. *(housekeeping — anytime)*
