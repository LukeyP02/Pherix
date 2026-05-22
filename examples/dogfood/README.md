# Pherix dogfood suite

Real LLM agents, with Pherix genuinely in the tool-call path — catching what
would hurt.

## Two distinct things — do not conflate them

Accuracy matters here: there are **two** kinds of artifact in this suite, and
they make different claims.

- **Mechanism tests** — `tests/test_dogfood_*.py`. A *mocked* Anthropic client
  emits a canned `tool_use` sequence; we assert that *given that exact sequence*
  the engine unwinds / audits / gates / isolates correctly. They are offline,
  deterministic, and run in CI. **They are not agents** and must not be
  described as "real-agent dogfoods" — they prove the wiring is correct, not
  that Pherix is useful against an unpredictable agent.
- **Real-agent runs** — `python -m examples.dogfood.*`. A *real* model is given
  a **goal** (not a step list) and decides for itself; the outcome is
  **genuine** — it depends on what the agent actually does, and a real run can
  succeed *or* fail. This is the demo and the first-user signal. It is
  operator-invoked (needs a key, hits the network) and is **not** a pytest test.

The mechanism tests already prove the engine is **correct** (savepoints restore,
the journal folds, the MCP wire matches the spec). The real-agent runs are the
only thing that shows Pherix is **useful**: a real model making real (sometimes
wrong) decisions, with Pherix wrapping every tool call, so that when the agent
does something that would hurt — ships an unbackfilled migration, races another
agent on one ledger, reaches for `/etc` — Pherix unwinds, gates, or isolates it.
Because the outcomes are **not rigged** (the devops smoke test computes health
from real state; the audit reconciliation depends on real arithmetic), running a
*batch* surfaces the genuine variance — see `capture.py` below.

## Two interception models (split by tool type)

- **Domain tools (DevOps, Audit).** The agent calls tools *we* define
  (`add_column`, `deploy`, `query_ledger`, …). They are `@tool`-wrapped and
  dispatched inside `agent_txn` / `dry_run` — the library's intended shape. The
  harness below provides this.
- **Built-in tools (Coding).** A coding CLI uses Edit/Write/Bash — built-ins
  that MCP can *add to* but cannot intercept. So the coding case runs an
  out-of-box CLI *inside an agent-agnostic sandbox*: a Pherix copy-on-write
  filesystem overlay plus shimmed `git`/shell on `PATH` that route through
  Pherix. Build once, govern everything — and it works for Claude Code, Cursor,
  or Gemini CLI alike, which a Claude-Code-only hook would not.

## The foundation

- **`harness.py`** — `run_agent(...)`, a thin tool-use loop that opens a
  transaction, runs the model inside it, and dispatches each tool call to the
  matching Pherix `@tool`. The chat protocol sits behind a backend seam:
  `api="anthropic"` (the Messages API) or `api="openai"` (any OpenAI-compatible
  local endpoint — Ollama / vLLM — via `base_url`). The Pherix dispatch is
  identical across both, which is the model-blindness proof. A policy denial is
  fed back to the model as a tool-result error so the agent *adapts* instead of
  crashing. Returns an `AgentRun` (transcript, journal, audit handle, final
  `TxnState`, and the `DryRunResult` in dry-run mode).
- **`infra.py`** — `scratch_sqlite` / `temp_tree` / `scratch_repo`: real but
  throwaway infrastructure, cleaned up on exit.
- **`capture.py`** — wraps `run_agent` to turn an ephemeral run into recordable
  evidence. It runs a dogfood (or a **batch** of N) and writes a structured
  report per run — the transcript, the Pherix journal, an explicit "here is what
  would have hurt and here is what Pherix did about it", and a verdict
  (`committed` / `contained` / `gated`) — plus a batch summary with the verdict
  distribution and **containment rate**. Batch mode is where the genuine
  variance shows up: how often a real agent slips, and how often Pherix catches
  it. Operator-run (`python -m examples.dogfood.capture devops --runs 4`).

The foundation is read-only to the dogfood streams: they import the harness,
they don't fork it.

## Running a dogfood

A cloud run needs an Anthropic key. Put it in `.env` at the repo root
(gitignored; `.env.example` is the tracked template):

```
ANTHROPIC_API_KEY=sk-ant-...
```

A **local** run needs no key — point the harness at an OpenAI-compatible local
endpoint (Ollama / vLLM). The devops dogfood exposes this as `--local`:

```
python -m examples.dogfood.devops --local \
    --base-url http://localhost:11434/v1 --model qwen2.5-coder:7b
```

The same release, the same atomic unwind, driven by a local open-source model —
Pherix is model-blind, so the governance is identical. The OpenClaw integration
(`examples/dogfood/coding/openclaw/`) and the air-gapped capstone
(`docs/operator/airgapped-capstone.md`) build on this.

Install the agent dependency (kept out of the dependency-free library):

```
pip install -e '.[dogfood,postgres]'   # postgres extra: the DevOps demo is Postgres-only
```

Then run a dogfood as a module from the repo root (these are the **real-agent
runs** — a real model, a goal, a genuine outcome):

```
python -m examples.dogfood.devops          # ship v2 on REAL Postgres: a healthy release commits, a careless one unwinds
python -m examples.dogfood.audit           # two concurrent agents reconcile a real imbalance, attributed + isolated
python -m examples.dogfood.coding redteam  # autonomous red-team: an overreaching agent's reaches are denied + gated (the OpenClaw demo)
```

The DevOps demo needs a real Postgres (`createdb pherix_dogfood` then export
`PHERIX_PG_DSN=postgresql://localhost/pherix_dogfood`) — a genuine `SAVEPOINT` is
the point. `python -m examples.dogfood.coding` (no `redteam`) is still the offline
mechanism walkthrough.

Or run a **batch** and get a comparable report + containment rate:

```
python -m examples.dogfood.capture devops --runs 4 --out reports/   # Postgres
python -m examples.dogfood.capture audit  --runs 4
python -m examples.dogfood.capture coding --runs 4                  # the red-team
```

Every real run writes its journal to `reports/<scenario>.audit.db` and prints how
to open it in the **inspector** (`python -m pherix.inspector --db ...`), where the
rollback / gate / audit trail renders at a glance. See
[`docs/operator/demos.md`](../../docs/operator/demos.md) for the full run-and-film
runbook. Default model is `claude-sonnet-4-6` — capable enough to make real
decisions, cheap enough to loop.

## Offline-test discipline

The pytest suite stays **fully offline**: no key, no network, no `anthropic`
import. The mechanism tests inject a mock client (`run_agent(..., client=...)`)
with a canned `tool_use` sequence. The real SDK is constructed lazily only when
no client is supplied. Real-agent runs are scripts the operator invokes by hand
— they are **not** pytest tests.

## The streams

| Stream | Dogfood | Real-agent run proves | Mechanism test guards |
|---|---|---|---|
| 1 | Integration + live MCP | the gateway works over a real subprocess boundary, not just by inspection | the wire protocol matches the spec |
| 2 | DevOps (migrate + deploy, **real Postgres**) | a real agent ships v2; a genuinely-unhealthy release (e.g. unbackfilled flag) unwinds atomically against a real `SAVEPOINT`, a healthy one commits | both branches of the smoke check given a scripted sequence (SQLite in CI; Postgres lane guarded in `test_dogfood_devops_postgres`) |
| 3 | Audit (reconciliation) | two concurrent agents reconcile a real imbalance without corruption, each attributed by `client_id` | attribution, balance, and the reviewer-vs-corrector isolation conflict |
| 4 | Coding red-team (sandbox / OpenClaw) | an autonomous overreaching agent's destructive reaches (deletes outside `src/`, secret clobber, push to `main`) are denied at the policy boundary and held at the commit gate; the real OpenClaw form is the air-gapped capstone | the harness-driven red-team (`test_dogfood_coding_redteam`) and the sandbox route (`test_dogfood_coding`) both gate + audit offline |
