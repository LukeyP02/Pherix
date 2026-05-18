# Pherix Roadmap

Slice-by-slice build status. The full spec for each slice lives in `CLAUDE.md`.

## Slices

- [x] **Slice 1** — SQL adapter (savepoints) + `Transaction` state machine + `agent_txn()` + SQLite audit journal + allow-list policy. Reversible path end-to-end. *(merged `ad4e9c0`)*
- [x] **Slice 2** — Filesystem adapter (copy-on-write) + `TransactionalResourceAdapter` sub-protocol + mixed-resource transactions. *(merged `c926f97`)*
- [x] **Slice 3** — HTTP/irreversible adapter — staging, `StagedResult`, gate, compensator-as-tool, mixed-fold partial-commit unwind with `STUCK` for missing compensators. *(merged `a13018b`)*
- [ ] **Slice 4** — Isolation — read/write-set tracking, conflict detection, resolution policy.
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
