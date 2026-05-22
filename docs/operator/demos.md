# Operator runbook — running and filming the four demos

This is the how-to for the dogfood demos: the four real-agent runs that turn
"what's Pherix?" into a design-partner conversation. Each is a **genuinely
autonomous** run — a real model given a *goal*, not a script — with Pherix in the
tool-call path, and each produces a journal the **inspector** renders so the
containment (rollback / gate / audit trail) reads at a glance.

> **Two artifacts, do not conflate.** The `tests/` suite holds the *mechanism
> tests* — mocked client, deterministic, offline, in CI. They prove the wiring is
> correct. The runs below are the *real-agent* demos — a real model, a genuine
> (sometimes failing) outcome, operator-invoked, **not** in CI. The footage comes
> from these. See `examples/dogfood/README.md` for the full split.

---

## One-time setup

```bash
pip install -e '.[dogfood,postgres]'      # agent SDKs + the psycopg driver
```

Cloud runs read `ANTHROPIC_API_KEY` from `.env` at the repo root (gitignored; copy
`.env.example`). Local runs need an OpenAI-compatible server (Ollama / vLLM) and
no key. The DevOps demo additionally needs a **real Postgres** (below).

Serve the inspector once, from the repo root, and point it at a run's journal:

```bash
python -m pherix.inspector --db reports/<scenario>.audit.db
# then open http://127.0.0.1:8765
```

Every demo writes its journal to `reports/<scenario>.audit.db` and prints this
exact command when it finishes. `reports/` is gitignored — it is generated
evidence, not source.

---

## Demo 1 — Atomic unwind (DevOps), on real Postgres

**Proves:** a real release agent ships v2; a careless one (adds the
`feature_flag` column but never backfills existing rows) trips a genuine
post-deploy smoke check *at commit-time*, and the whole transaction unwinds — the
irreversible deploy is compensated, the migration/backfill/config restored from
their snapshots. A thorough agent commits. The outcome is genuine: it depends on
what the agent did, not a flag.

**Postgres-only by design.** A real `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` against
a real server is the point — SQLite alone reads as a toy. Stand one up and export
a DSN:

```bash
createdb pherix_dogfood                                 # local Postgres
export PHERIX_PG_DSN=postgresql://localhost/pherix_dogfood
```

Each run gets its own disposable schema (dropped on exit), so the server can be
shared and nothing leaks between runs.

```bash
python -m examples.dogfood.devops            # cloud Anthropic
python -m examples.dogfood.devops --local \  # local model — same unwind
    --base-url http://localhost:11434/v1 --model qwen2.5-coder:7b
```

**Phase 1** is a dry-run preview (the agent plans against a snapshot; the
`state_diff` and the would-fire irreversibles print, nothing commits). **Phase 2**
is a batch of real releases; the run prints a verdict per release and the
**containment rate**.

**What to film:** the batch summary (some `committed`, some `contained`), then the
inspector showing a contained run — the `deploy` row struck through
(COMPENSATED), `smoke_test` FAILED, the reversibles undone, the transaction
`ROLLED_BACK`.

---

## Demo 2 — Attributed audit (concurrent reconciliation)

**Proves:** two agents reconcile one ledger *concurrently*, each under its own
`client_id`. Every adjustment is attributed (in the journal and on the row), the
source entries survive uncorrupted, and if the two race on the same entry the
Abort isolation policy unwinds the second committer. Whether the books reach zero
depends on what each agent computed — the honest variance.

```bash
python -m examples.dogfood.audit
```

**What to film:** the per-`client_id` compliance view printed at the end, then the
inspector filtered by `client_id` — two transactions, attributed, isolated; an
aborted one shown `ROLLED_BACK` if a race occurred.

For a batch + containment rate:

```bash
python -m examples.dogfood.capture audit --runs 4
```

---

## Demo 3 / 4 — The coding red-team (and the OpenClaw capstone)

**Proves:** a real model given an aggressive "slim the repo and ship it" goal
*overreaches* — deletes outside `src/`, clobbers a secret (`.env`), pushes to
`main`, reaches for irreversible git/shell — and Pherix contains every reach: the
out-of-bounds actions are **denied at the policy boundary** (stage-time, nothing
journalled), the allowed irreversibles are **held at the commit gate** (no
compensator, no approval — nothing fires), and only in-`src` edits could ever
apply (and roll back when the gate blocks commit). This is the realistic threat —
an over-eager automation, not a cartoon villain.

**Autonomous run (today, no special hardware):**

```bash
python -m examples.dogfood.coding redteam                 # cloud Anthropic
python -m examples.dogfood.coding redteam --local \        # local model
    --base-url http://localhost:11434/v1 --model qwen2.5-coder:7b
```

The `client_id` is the OpenClaw identity (`openclaw@airgapped-box`): this *is* the
OpenClaw red-team, driven through the harness. Running it `--local` is the
air-gapped configuration in miniature — a local model, local governance, local
audit.

**What to film:** the batch verdicts (`contained`), the per-run "what would hurt /
what Pherix did" narration, then the inspector showing the denied calls and the
GATED irreversibles — nothing destructive ever touched the filesystem.

**The full OpenClaw capstone** — driving the real OpenClaw daemon on a local model
inside the sandbox, on a wifi-off disposable box, across *both* interception
surfaces (MCP domain tools + the environment sandbox) — is its own protocol:
see [`airgapped-capstone.md`](airgapped-capstone.md). Run the autonomous red-team
above first as the warm-up and the automated evidence; the capstone is the
manual, on-camera finale.

---

## Capturing evidence (batches + reports)

`capture.py` turns ephemeral runs into comparable evidence — a per-run report
(transcript, journal, an explicit "what would have hurt / what Pherix did", a
verdict) plus a batch summary with the verdict distribution and **containment
rate**. The batch is where the genuine variance shows.

```bash
python -m examples.dogfood.capture devops --runs 4 --out reports/   # Postgres
python -m examples.dogfood.capture audit  --runs 4 --out reports/
python -m examples.dogfood.capture coding --runs 4 --out reports/   # red-team
```

`--out` writes one JSON per run + a summary. The shared inspector journal is
written automatically (use `--no-journal` to skip it). Open it with the printed
`python -m pherix.inspector --db reports/<scenario>.audit.db`.

---

## Quick reference

| Demo | Command | Verdict you want | Needs |
|---|---|---|---|
| Atomic unwind | `python -m examples.dogfood.devops` | mix of `committed` / `contained` | key + Postgres DSN |
| Attributed audit | `python -m examples.dogfood.audit` | attributed + isolated | key |
| Coding red-team | `python -m examples.dogfood.coding redteam` | `contained` | key (or `--local`) |
| OpenClaw capstone | [`airgapped-capstone.md`](airgapped-capstone.md) | gated + audited | local model + OpenClaw |
