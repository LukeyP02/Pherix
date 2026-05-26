# Pherix Roadmap

Slice-by-slice build status. The full spec for each slice lives in `CLAUDE.md`.

## Slices

- [x] **Slice 1** — SQL adapter (savepoints) + `Transaction` state machine + `agent_txn()` + SQLite audit journal + allow-list policy. Reversible path end-to-end. *(merged `ad4e9c0`)*
- [x] **Slice 2** — Filesystem adapter (copy-on-write) + `TransactionalResourceAdapter` sub-protocol + mixed-resource transactions. *(merged `c926f97`)*
- [x] **Slice 3** — HTTP/irreversible adapter — staging, `StagedResult`, gate, compensator-as-tool, mixed-fold partial-commit unwind with `STUCK` for missing compensators. *(merged `a13018b`)*
- [x] **Slice 4** — Isolation — read/write-set tracking, commit-time conflict diff, resolution policy (`Abort` / `Retry(N)` / `Serialize`), in-process `JournalRegistry` and filesystem-shared SQLite cross-process arbitration via the `SQLiteAdapter` meta-connection. *(merged `0fab344`)*
- [x] **Slice 5** — Replay from the journal — top-level `pherix.replay(txn_id, adapters, source_audit=..., mode='verify'|'reconstruct')` folds the journal forward against operator-supplied fresh adapters. Default JSON comparator with per-tool `@tool(comparator=fn)` escape hatch; irreversible APPLIED effects are skip-and-reused; `Transaction.replayed_from` links replay txns to their source. Demo at `examples/slice5_demo.py`.
- [x] **Slice 6** — Real policy engine — `@policy.rule` Python callables returning `Allow | Deny(reason)`, `Cap.count(tool, max)` and `Cap.sum(tool, via, max)` spend-cap primitives, runtime-owned `PolicyContext` carrying journal-by-reference + per-cap running totals, stage/commit evaluation bracket (`evaluate` at stage-time + `evaluate_journal` at commit-time before the staged-fire loop — preempts irreversibles under commit-time denial), enriched `PolicyViolation` with `where`/`rule`/`reason`/`effect_index`. World-state-aware rules deferred to Slice 6.5 — `ctx.read(...)` ships as a `NotImplementedError` placeholder seam. Demo at `examples/slice6_demo.py`. *(merged `c2318d5`)*
- [x] **Slice 7** — Speculative dry-run — top-level `pherix.dry_run(adapters)` context manager that runs the agent body identically to `agent_txn`, captures policy verdicts instead of raising, then unwinds via the existing snapshot/rollback bracket. World is bit-identical pre/post. `DryRunResult` carries the journal, the `would_have_fired` filter (staged irreversibles), the captured `policy_verdicts` (stage-time + commit-time), and `is_clean`. `Policy.try_evaluate` + `Policy.collect_verdicts` are capture-mode siblings of `evaluate` / `evaluate_journal` (the load-bearing equality: caps only accumulate on Allow, same as raise-mode). Audit gains a `dry_run` column. Per-adapter structured state diff and concurrency-aware dry-run deferred to Slice 7.5. Demo at `examples/slice7_demo.py`. *(merged `6ee8980`)*
- [ ] **Slice 8** — MCP gateway front-end. The test of the frontend-agnostic discipline: same engine, same journal, MCP protocol as the driver. Absorbs the Slice-7 deferred state-diff work — the gateway is the consumer that makes structured per-adapter state diff genuinely necessary (a library caller can introspect the journal directly; a wire-format consumer needs the diff pre-computed). Concurrency-aware dry-run (Slice 7 Strand B) dropped — the v1 use case doesn't have it, and post-Slice-8 multi-agent contexts will design cross-process semantics in the gateway's terms.

## Follow-ups from Slice 1 review

Captured from the `feat/slice-1` review — decide before the slice noted.

- ~~**Non-serializable args silently `str()`-coerced**~~ Resolved before Slice 3: `effects.strict_json_default` supports `bytes` (base64), `datetime` (ISO 8601), and any `@dataclass` (recursive `asdict`); anything else raises `EffectArgsError` at `Effect` construction. `audit.py` shares the same serialiser. Idempotency keys are now collision-safe by construction across distinct non-trivial types.
- ~~**Adapter lifecycle is duck-typed, not in the Protocol.**~~ Resolved in Slice 2: `TransactionalResourceAdapter` sub-protocol introduced in `adapters/base.py`; `runtime.py` dispatches lifecycle via `isinstance` rather than `hasattr`. A typo'd `begin` now fails at type-check rather than silently skipping.
- **`_guard_thread` comment oversells coverage.** It catches explicit sharing of the `TxnContext` across threads (good, tested), but not the silent case — a tool dispatched to a worker thread where `active_txn` is empty, so the wrapper runs it raw. Mitigation is reasonable; tighten the comment so it doesn't imply full coverage. *(nit — anytime)*
- ~~**Default `AuditJournal()` is `:memory:`.**~~ Resolved before Slice 5 (`1c66f70`): `AuditJournal(path)` now requires an explicit path; ephemeral runs opt in via `AuditJournal.in_memory()`. The visible call site forces a deliberate durability choice.

## Follow-ups from Slice 3 review

- **Compensator contract is args-only, not result-piped.** `_partial_unwind` invokes `compensator(*effect.args)` — the original tool's *return value* (e.g. a real Stripe `charge_id`) is in the journal but isn't piped into the compensator. Today's mitigation: design tools to re-derive identity from args (use Pherix's `effect_id` as an upstream idempotency key, look up the original by that key inside the compensator). Proposed extension: opt-in `@tool(compensator='n', pipe_result=True)` form — compensator signature becomes `(result, **original_args)`, backwards-compatible default. *(before first real backend integration — the obvious Stripe wiring trips on this)*
- **Compensator execution isn't journalled as a separate row.** `_partial_unwind` constructs a synthetic `Effect(index=-1, ...)` to carry the compensator's fire through `adapter.apply`; the original effect's status flips `APPLIED → COMPENSATED` in place, but the compensator's own execution isn't an audit row. Auditability concern for Slice 8++ (the audit pillar): "when did the refund actually fire?" can't be answered from the journal alone. Decide: append-the-compensator-as-a-real-effect, or extend the row schema with a `compensator_ts` column, or accept the current compression. *(audit-pillar concern)*
- ~~**Idempotency test is a pin, not a scenario.**~~ Retired by Slice 5: irreversible-`APPLIED` skip-and-reuse is now exercised by the real replay walker against a deterministic source journal (`test_irreversible_applied_is_never_refired_on_replay`, `test_reconstruct_also_skips_already_applied_irreversibles`). The status-flip pin is no longer the only witness.

## Follow-ups from Slice 4

Captured during Slice 4 implementation; decide before Slice 5 lands or the noted slice.

- ~~**Monotonic-counter MVCC cannot distinguish self-bumps from cross-txn writes.**~~ Resolved before Slice 5 (`10b461f`): `write_keys` now carries `(resource, key, version_after_my_write)`. The commit-time diff folds via `last_my_write[(resource, key)]` to pick the most recent self-bump, so a cross-txn write that lands AFTER our last bump correctly registers as a conflict instead of being filtered by the old own-writes set.
- ~~**Audit journal does not persist `read_keys` / `write_keys`.**~~ Resolved before Slice 5 (`f283eeb`): two `TEXT NOT NULL DEFAULT '[]'` columns on the `effects` table; `_dump`-pass at `record_effect` and `update_effect`. Slice 5's replay engine reads them back via `audit.get_effects(txn_id)` and rebuilds the in-memory `Effect.read_keys` / `write_keys` for the replay txn's own commit-time isolation diff.
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

## Policy axis — over-containment as a tuning system (post-MVP)

**The objection, from the market.** The sharpest critique of any containment layer (seen verbatim in the r/AI_Agents "74% rolled back" thread, 2026-05): *"the whole point of agents is to function without supervision — gate everything and you've just rebuilt a supervised script, which doesn't justify the spend."* If Pherix gates indiscriminately it destroys the autonomy that made the agent worth deploying. This is the central Policy-axis risk, and the answer is **gate proportional to blast radius, not uniformly.**

**The MVP stays slim — the primitives already exist.** Over-containment is a *tuning* problem, and every lever to keep containment cost near-zero is already shipped, so nothing here is MVP work:
- reversible lane = free autonomy (snapshot/restore, no gate — most routine work);
- compensators = irreversible-but-invertible actions auto-commit (no human, inverse only on rollback);
- policy caps/thresholds = graduated clearing (`refund < $100` auto, `> $1000` gate);
- dry-run = approve-the-plan-once instead of interrupting per step.

The maths: if action-risk is heavy-tailed (most actions trivial/reversible, few catastrophic), gating only the tail captures ~all the danger at ~zero autonomy cost. Containment is cheap *because* risk is concentrated.

**What is roadmap (pulled by a design partner, not built ahead of one):**
- **Proportional-policy ergonomics** — make "gate by stakes" a first-class expression the buyer writes against, not hand-rolled rules. The dial should be legible.
- **Trust escalation** — policy that *widens* as the journal proves behaviour: start tightly gated, loosen on clean history (the employee-autonomy curve). The journal is the trust record that justifies the loosening — a fold over the audit log, not a new mechanism.
- **Containment observability** — surface the *gate rate* and *escape rate* so over- vs under-containment is measurable. Over-containment = high gate rate + ~zero escape rate (stopping things that didn't need it). Both numbers are already in the journal; this is a read, not an engine change.

**Positioning / ICP note (not a build):** Pherix de-risks best where the dangerous actions are *rare relative to routine ones* (heavy-tailed). An agent that is 95% recoverable work + a rare irreversible cliff is the sweet spot; an agent whose value is uniformly irreversible-and-high-stakes (e.g. a pure trading agent) cannot be de-risked without neutering it — flag these as poor-fit in discovery. *(decide alongside design-partner conversations — the "what fraction of your agent's high-value actions are irreversible and uncompensatable?" probe is what sizes this per buyer)*
