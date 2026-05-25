# The air-gapped flagship — data sovereignty, governed offline

> The single most powerful sentence for a regulated enterprise:
> **the agent runs on a local open model, the regulated data never leaves the
> perimeter, and Pherix governs it offline.**

This example points the *same* frozen regulated-data-ops agent from the
enterprise sim (`examples/dogfood/sims/enterprise/`) — same system prompt, same
toolset, same enterprise policy — at a **local** OpenAI-compatible model server
(Ollama / vLLM / LM Studio) instead of a cloud API. Nothing else changes. That is
the whole demonstration.

## What stays inside the perimeter

```
        ┌──────────────── the regulated perimeter (one machine / one VLAN) ─────────────────┐
        │                                                                                    │
        │   ┌─────────────┐        tool calls          ┌──────────┐      SAVEPOINT / gate    │
        │   │ local model │ ───────────────────────▶   │  Pherix  │ ───────────────────────▶ │   customer DB
        │   │  (Ollama)   │ ◀───────────────────────   │  (journal│ ◀───────────────────────  │   + egress lane
        │   │  llama3.1   │     governed results        │ + policy)│      restore on rollback  │
        │   └─────────────┘                             └──────────┘                          │
        │                                                                                    │
        └──────────────────────── nothing crosses this line ────────────────────────────────┘
                          no model API call · no data egress · no telemetry
```

Three things that would normally leave the building — and don't:

1. **The model inference.** The brain is open weights running on local hardware.
   No prompt, no tool result, no customer PII is sent to `api.anthropic.com` or
   `api.openai.com`. There is no cloud model in the loop to send it to.
2. **The regulated data.** It lives in the local system of record (here a SQLite
   database of customer PII + financial ledger). Pherix's adapters snapshot and
   restore it *in place*; the irreversible egress lane stages and **gates** any
   export, so data only leaves on an explicit human sign-off — and in this demo
   it never does.
3. **The governance.** Pherix is a dependency-free library running in the same
   process. The journal, the policy evaluation, the snapshot/rollback — all of it
   is local. There is no SaaS control plane to phone home to.

## Why this matters to a regulated buyer

A bank, insurer, or hospital cannot send customer records to a third-party model
API — that is often the end of the conversation before it starts. The usual
answer is "run an open model locally," but that only solves *where the model
runs*. It does nothing about the agent's **side effects**: the local model can
still delete a record under legal hold, mutate a posted ledger entry, or export
the whole customer base to the wrong destination. A local model is still an
*ungoverned* agent.

Pherix closes that gap *inside the same perimeter*. The local model proposes tool
calls; Pherix intercepts them, checks them against the enterprise policy
(legal-hold protection, the egress allowlist, ledger immutability, bulk-delete
caps), applies the reversible ones behind a snapshot, and stages the irreversible
ones behind a gate. Data sovereignty and operational safety are solved by the
*same* deployment, with nothing crossing the boundary.

## Model-blindness — the same governed journal, local or cloud

Pherix wraps the **tool-call layer**, not the model. The backend seam in the
harness (`api="openai"` + `base_url`) swaps a local chat-completions endpoint in
for the cloud Messages API; everything behind it — the effect journal, the
policy, the adapters — is byte-identical. The offline test
(`tests/test_local_airgap.py`) proves this directly: it drives the frozen agent
through the local (OpenAI-compatible) backend and the cloud (Anthropic) backend
with the same scripted tool call and asserts the **two journals are equal**. So
the governance you validate on cloud Claude is exactly the governance you get on
a local Llama. The model is interchangeable; the guarantees are not.

## The sovereignty claim is *verified*, not asserted

A demo that merely *says* "nothing left the perimeter" is worthless. The capture
script (`capture_airgap.py`) **measures** it: it wraps the whole governed run in
an `EgressGuard` that records every TCP peer any socket in the process connects
to, then asserts that not one of them is a public-internet address (only
loopback / private ranges are inside the perimeter). A stray call to a cloud
model API would land a globally-routable IP in the recorded set and **fail the
capture**. The claim cannot pass by accident — it is checked against what the
process actually did at the socket layer.

## Running it

The live run is **infra-gated**, not code-gated: it needs a dedicated machine
(~16GB) running a local model, because a typical laptop OOMs hosting one
alongside this work. The code is built and tested offline regardless; the live
leg skips cleanly when no endpoint is configured.

```bash
# On the box with the local model (Ollama shown):
ollama serve &
ollama pull llama3.1:8b

# Run the frozen enterprise agent against it, governed:
LOCAL_MODEL_URL=http://localhost:11434/v1 LOCAL_MODEL=llama3.1:8b \
  python -m examples.local_airgap.run_local            # default fixture: dsar_export
LOCAL_MODEL_URL=http://localhost:11434/v1 \
  python -m examples.local_airgap.run_local ledger_recon

# Capture it as demo evidence — and verify no egress left the perimeter:
LOCAL_MODEL_URL=http://localhost:11434/v1 LOCAL_MODEL=llama3.1:8b \
  python -m examples.local_airgap.capture_airgap
# → prints the peers contacted (all loopback), the agent's actions, the journal,
#   writes reports/airgap-<fixture>.evidence.json, and points the governance
#   console at the persisted journal:
python -m pherix.inspector --db reports/airgap-<fixture>.audit.db
```

With no `LOCAL_MODEL_URL` set, both scripts print a clear skip message and exit
0. The offline proof — model-blindness, the egress guard, the governed journal —
runs in `tests/test_local_airgap.py` either way.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `LOCAL_MODEL_URL` | The local OpenAI-compatible endpoint. **Unset → skip.** | — |
| `LOCAL_MODEL` | The model id / tag. | `llama3.1:8b` |
| `LOCAL_AIRGAP_FIXTURE` | Which enterprise situation to run. | `dsar_export` |

The available fixtures are the enterprise sim's: `retention_cleanup`,
`dsar_export`, `ledger_recon`, `account_tidy`, `benign_control`.
