# Pherix dogfood suite

Real LLM agents, with Pherix genuinely in the tool-call path — catching what
would hurt.

## Why this exists (real agents, not scripts)

The 331-test suite already proves the *mechanism*: savepoints restore, policy
denies, the journal folds, the MCP wire matches the spec. That is deterministic
proof that the engine is **correct**. It says nothing about whether Pherix is
**useful**.

A dogfood is the opposite of a scripted demo. It is a real model making real
(sometimes wrong) decisions in a tool-use loop, with Pherix wrapping every tool
call. When the agent does something that would hurt — a deploy whose smoke test
fails, a coding CLI reaching for `/etc`, two agents racing on one ledger —
Pherix unwinds, gates, or isolates it, and we get to *watch* that happen. A
scripted "dogfood" would just be a redundant integration test.

## Two interception models (split by tool type)

- **Domain tools (DevOps, Audit).** The agent calls tools *we* define
  (`run_migration`, `deploy`, `query_ledger`, …). They are `@tool`-wrapped and
  dispatched inside `agent_txn` / `dry_run` — the library's intended shape. The
  harness below provides this.
- **Built-in tools (Coding).** A coding CLI uses Edit/Write/Bash — built-ins
  that MCP can *add to* but cannot intercept. So the coding case runs an
  out-of-box CLI *inside an agent-agnostic sandbox*: a Pherix copy-on-write
  filesystem overlay plus shimmed `git`/shell on `PATH` that route through
  Pherix. Build once, govern everything — and it works for Claude Code, Cursor,
  or Gemini CLI alike, which a Claude-Code-only hook would not.

## The foundation

- **`harness.py`** — `run_agent(...)`, a thin Anthropic tool-use loop that opens
  a transaction, runs the model inside it, and dispatches each `tool_use` to the
  matching Pherix `@tool`. A policy denial is fed back to the model as a
  `tool_result` error so the agent *adapts* instead of crashing. Returns an
  `AgentRun` (transcript, journal, audit handle, final `TxnState`, and the
  `DryRunResult` in dry-run mode).
- **`infra.py`** — `scratch_sqlite` / `temp_tree` / `scratch_repo`: real but
  throwaway infrastructure, cleaned up on exit.

The foundation is read-only to the dogfood streams: they import the harness,
they don't fork it.

## Running a dogfood

Real runs need an Anthropic key. Put it in `.env` at the repo root (gitignored;
`.env.example` is the tracked template):

```
ANTHROPIC_API_KEY=sk-ant-...
```

Install the agent dependency (kept out of the dependency-free library):

```
pip install -e '.[dogfood]'
```

Then run a dogfood as a module from the repo root:

```
python -m examples.dogfood.devops      # migration + deploy, unwound atomically
python -m examples.dogfood.audit       # two concurrent agents, attributed + isolated
python -m examples.dogfood.coding      # out-of-box CLI governed inside the sandbox
```

Default model is `claude-sonnet-4-6` — capable enough to make real decisions,
cheap enough to loop.

## Offline-test discipline

The pytest suite stays **fully offline**: no key, no network, no `anthropic`
import. Tests inject a mock client (`run_agent(..., client=...)`) with a canned
`tool_use` sequence. The real SDK is constructed lazily only when no client is
supplied. Real keyed agent runs are scripts the operator invokes by hand — they
are **not** pytest tests.

## The streams

| Stream | Dogfood | What it proves |
|---|---|---|
| 1 | Integration + live MCP | the gateway works over a real subprocess boundary, not just by inspection |
| 2 | DevOps (migrate + deploy) | a failing smoke test unwinds the whole release atomically |
| 3 | Audit (reconciliation) | two concurrent agents reconcile without corruption, each attributed by `client_id` |
| 4 | Coding (sandbox) | an out-of-box CLI is governed at the environment level, agent-agnostic |
