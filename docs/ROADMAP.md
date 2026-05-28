# Pherix Roadmap

The engine is built. The work now is making it visible and adoptable
without compromising the moat that keeps us out of customer data.

---

## Phase 1 — Engine (DONE)

The runtime substrate: every capability is a traversal of one journal.

- [x] Two lanes — reversible (snapshot/restore) + irreversible (stage/gate or compensate)
- [x] MVCC-style isolation between concurrent agents
- [x] Deterministic replay against fresh state
- [x] Policy — evaluated at stage-time *and* commit-time (TOCTOU-safe)
- [x] Dry-run / speculation against a snapshot
- [x] MCP gateway — any-language agents hit the same engine
- [x] Crash-consistent recovery driven from the durable journal
- [x] World-state policy via `PolicyContext.read`
- [x] Longitudinal envelope (`core/envelope.py`)
- [x] 16 Python adapters; 14 TypeScript at semantic + field-name parity
- [x] Vetted compensator catalog at module parity in both languages
- [x] 936 Python + 210 TypeScript tests, fully offline

**Open follow-ups** (deliberately scoped out of the engine pass):

- [ ] `sql.py` `execute_isolated` — materialise cursor via `.fetchall()` (latent Python correctness bug)
- [ ] TS `SqliteAdapter.readsCommittedOnly()` — hardcoded `false`, awaits TS meta-connection for on-disk cross-process isolation parity

---

## Phase 2 — Make it visible (ACTIVE)

Build to the **base**: the things any buyer assumes work. Let the
design partner bring the edge cases.

### The laptop demo — six baseline stories

A single 5-minute script that buyers run on their own machine.
Each story lands a different buyer; together they prove this is a
runtime, not a rollback library.

- [ ] **Rollback on failure** — adapter restore. *"Our agent broke prod last week."*
- [ ] **Isolation under concurrency** — N agents racing, conflict caught. *"50 agents step on each other."*
- [ ] **Policy / capability enforcement** — forbidden call blocked at stage time. *Compliance / security / platform.*
- [ ] **Irreversible gating** — money / email / external API paused for approval. *"More autonomy, last-mile trust."*
- [ ] **Dry-run / speculation** — show what the agent would do. *Reviewer / planning UIs.*
- [ ] **Replay + audit** — re-run yesterday's run on fresh state. *Compliance / debugging / regression.*

### Five additional engine stories

The capabilities that already exist in the engine but don't yet have a
buyer-facing demo. Each is a differentiator vs the obvious competitors.

- [ ] **Cross-language parity** — Python service + TS edge worker on one journal.
- [ ] **MCP gateway / any-language** — Go/Rust/Java agents, same guarantees, no SDK.
- [ ] **Crash recovery** — kill mid-txn, restart, fold the durable journal, end consistent.
- [ ] **World-state policy** — predicate evaluated against live state at commit time.
- [ ] **Journal-as-truth vs LLM transcript** — agent claims X but journal disagrees. Security/audit angle.

### Distribution levers

How the Saturday-afternoon user discovers and tries Pherix.

- [ ] One ecosystem integration — pick one: LangGraph / OpenAI Agents SDK / Anthropic Agent SDK / Mastra
- [ ] Migration guide — "naive tool calling → Pherix in 5 lines"

### Operational proof points

The questions every buyer asks before deployment. Own the numbers
before they're asked.

- [ ] Latency / throughput overhead benchmark per tool call
- [ ] Compensator catalog growth — Stripe, SendGrid, Twilio, AWS-side-effects, …
- [ ] Multi-tenant isolation for SaaS-agent companies

---

## Phase 3 — Paid hosted control plane (PENDING — design-partner-pulled)

The OSS does single-host. The paid tier is the things that
**inherently** need centralised infrastructure to exist. Not built ahead
of demand; pulled in by the first design partner.

- [ ] Cross-host arbiter (multi-region, multi-agent coordination)
- [ ] Audit search across many agents at org scale
- [ ] Anonymised policy library aggregating across tenants

**Constraint**: metadata-only. The control plane sees txn ids, effect
shapes, policy verdicts, conflict witnesses — never row values, file
contents, or HTTP payloads. The data-untouched moat is preserved across
both tiers.

---

## Phase 4+ — Future-bets (FUTURE)

Signposted on the trajectory, not committed.

- [ ] **Agent memory as an adapter** — governed/auditable memory mutations. Sleeper differentiator; the spec is already implied by the four axes.
- [ ] Long-running / checkpointing for hours-long autonomous agents
- [ ] Hosted control plane preview UI (mocked) — plants the upgrade seed before the plane ships

---

## Positioning (the strategic context that makes the roadmap make sense)

- **AI-native infrastructure, not an AI-native application.** Pherix only
  exists because of the agent moment, but it sells shovels to the
  miners — Sierra, Decagon, Devin et al. are customers, not competitors.
- **Open-core, not freemium.** OSS gets every capability that runs on a
  single host. Paid is hosted infrastructure that genuinely can't be
  self-hosted usefully (cross-host arbiter, cross-tenant aggregation).
  Never gate single-host features behind paid.
- **Small teams are the funnel, not a leak.** ~99% never pay; the ~1%
  that does funds the whole thing. The Saturday-afternoon user is the
  same person who buys the enterprise tier 12 months later.
- **Data-untouched moat.** The laptop demo runs on the buyer's machine;
  the hosted plane sees only metadata. Hosting cost tracks revenue by
  construction — zero customers means zero infra to run.
