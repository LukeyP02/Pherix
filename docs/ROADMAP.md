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

## Beyond Slice 8 — interception-surface discussion points

The existing post-Slice-8 trilogy (determinism + memory + audit) frames *what the engine extends to do*. These three are a different axis — *where the engine intercepts*. Surfaced when a real corporate decision (cancelling a coding-CLI tool over "guardrails concerns") landed exactly on Pherix's wedge. Same engine, same journal, same compensator-as-tool pattern at each layer:

```
agent (Claude Code, Cursor, Aider, …)
   ↓  ← Slice 8 MCP gateway (intercepts agent tool calls)
coding tool calls (Edit, Write, Bash, MCP tools)
   ↓  ← Slice 9 candidate: GitAdapter (intercepts git ops specifically)
git operations (commit, push, branch, PR)
   ↓  ← Slice 10+ candidate: Pherix workflow primitive (intercepts pipeline steps)
deployment / infrastructure
   ↓  ← Existing Slice 1–3 adapters
real resources (SQL, FS, HTTP, k8s)
```

- **Slice 8 sharpening — first paying user is a security team allowing coding-CLI provisioning.** The existing Slice 8 brief is generic ("MCP gateway frontend"). The cancelled-Claude-Code signal sharpens the first specific user: a security team wanting to *permit* coding-CLI tools (Claude Code, Aider, Cursor agentic mode) by putting Pherix in front of them via MCP. Acceptance criterion stops being "the gateway works for any MCP-speaking agent" and becomes "a security team would let Claude Code be provisioned because Pherix is in front of it." Artifacts to produce alongside Slice 8: gateway config templates for common coding-tool MCPs, gate-policy templates for shell / file / git operations, audit-row schema covering bash + git + file events. *(close inside Slice 8 — not a separate slice)*
- **GitAdapter as a Slice 9+ candidate.** Every git operation has a natural semantic left-inverse — `git commit` ↔ `git reset --soft HEAD~1`; `git push` ↔ `git push --force-with-lease origin <branch>:<previous-sha>`; `gh pr create` ↔ `gh pr close`; `git merge` ↔ `git reset --hard <merge-base>`. The compensator-as-tool pattern from Slice 3 maps onto git directly. A `GitAdapter` would let any code agent (Slice 8-wrapped or using git directly) inherit transactional semantics for code-changes. **Open design questions:** does it sit as a single adapter or a family (`LocalGitAdapter` for working tree + `GitRemoteAdapter` for push/PR)? Does it compose with `FilesystemAdapter` (git operates on files) or shadow it? How do `reflog` / `stash` interact with snapshot semantics — are they Pherix's snapshot mechanism for free, or do they confuse the journal? *(decide alongside the dogfood prototype work where git becomes a stress surface — pick #2 migration bot exercises this first)*
- **CI/CD as a Pherix workflow primitive — Slice 10+ or distinct product line.** A CI pipeline step `uses: pherix/transaction@v1` wraps the pipeline body in `agent_txn`; deploys stage with operator-supplied compensators (`kubectl apply` ↔ `kubectl rollout undo`; `aws cloudformation deploy` ↔ `aws cloudformation rollback`); failed post-deploy smoke tests fire compensators automatically. Genuinely different deployment model from in-process library and MCP gateway — *Pherix as a workflow primitive*, where the "agent" being intercepted is the pipeline itself, not an LLM. **Open design questions:** distinct product (own packaging, GitHub Action, GitLab template) or Slice 10 of the engine? Which CI systems first (GitHub Actions has the largest reach; Buildkite is friendliest to custom step types)? How does the Slice 8 gateway's cross-host arbitration interact with multi-stage pipelines that span runners? Is the CI-step abstraction just a thin shim over the existing `agent_txn` API, or does it need its own state-machine vocabulary? *(decide once Slice 8 ships — gateway dependency is real, and CI-as-pipeline closely mirrors the MCP-as-protocol shape Slice 8 is already solving)*

The strategic frame: these aren't separate integrations. They are three depths of the same wedge — *Pherix as the layer between an agent and any tool that touches code.* The corporate AI-tooling cancellation case becomes "deploy the agent through Pherix at one or more of these layers, not directly." Every layer is the same engine; each one is a different point of interception.
