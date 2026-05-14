# Pherix Roadmap

Slice-by-slice build status. The full spec for each slice lives in `CLAUDE.md`.

## Slices

- [x] **Slice 1** — SQL adapter (savepoints) + `Transaction` state machine + `agent_txn()` + SQLite audit journal + allow-list policy. Reversible path end-to-end. *(merged `ad4e9c0`)*
- [ ] **Slice 2** — Filesystem adapter (copy-on-write).
- [ ] **Slice 3** — HTTP/irreversible adapter — staging, `StagedResult`, gate, compensation registry, idempotency keys.
- [ ] **Slice 4** — Isolation — read/write-set tracking, conflict detection, resolution policy.
- [ ] **Slice 5** — Replay from the journal.
- [ ] **Slice 6** — Real policy engine — capability grants, spend caps, content-aware rules, commit-time re-eval.
- [ ] **Slice 7** — Speculative dry-run diff.
- [ ] **Slice 8** — MCP gateway front-end.

## Follow-ups from Slice 1 review

Captured from the `feat/slice-1` review — decide before the slice noted.

- **Non-serializable args silently `str()`-coerced** (`effects.py` `compute_effect_id`, `audit.py` `_json_default`). Planning called for enforcing JSON-serializable args at `Effect` construction with a clear error; instead they're stringified. Risk: distinct non-serializable objects can collide on `effect_id`, and the audit row is lossy. Decide: enforce-and-raise, or consciously accept the coercion. *(before Slice 3 — idempotency keys become load-bearing there)*
- **Adapter lifecycle is duck-typed, not in the Protocol.** `runtime.py` calls `begin`/`commit`/`rollback` via `hasattr`; the `ResourceAdapter` protocol in `adapters/base.py` only declares `snapshot`/`apply`/`restore`/`supports_rollback`. Decide whether txn-scope lifecycle belongs in the protocol (likely an optional sub-protocol). *(before Slice 2 — `FilesystemAdapter` needs the same lifecycle)*
- **`_guard_thread` comment oversells coverage.** It catches explicit sharing of the `TxnContext` across threads (good, tested), but not the silent case — a tool dispatched to a worker thread where `active_txn` is empty, so the wrapper runs it raw. Mitigation is reasonable; tighten the comment so it doesn't imply full coverage. *(nit — anytime)*
- **Default `AuditJournal()` is `:memory:`.** Fine for tests/demo, but durability is a stated core property — a non-durable default journal is an odd default. Decide: on-disk default, or keep in-memory with explicit opt-in. *(before Slice 5 — replay needs a durable journal to replay from)*
