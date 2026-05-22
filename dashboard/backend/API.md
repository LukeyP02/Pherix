# Pherix Control Plane — v1 API contract

The multi-tenant commercial service (`dashboard/backend/`). It **consumes the
substrate** (the audit journal shapes) and never reaches into the engine. The OSS
SDK does not depend on it; journal-ship is opt-in and the library runs fully
offline without it.

This doc is the human-readable summary. The live, authoritative contract is the
OpenAPI served at **`/api/docs`** (`/api/openapi.json`) when the service runs.
The operator owns the frontend/UI and may own the public contract surface — this
documents what the *service layer* exposes so the two can align.

## Running

```bash
pip install ".[control-plane]"
PHERIX_CP_DB=/path/cp.db PHERIX_CP_ADMIN_KEY=<secret> \
  uvicorn dashboard.backend.app:app
```

`create_app(store, admin_key)` is the factory; the module-level `app` reads
`PHERIX_CP_DB` (SQLite path) and `PHERIX_CP_ADMIN_KEY` (bootstrap secret) from env.

## Auth

Two bearer-token trust levels, in the `Authorization: Bearer <token>` header.

| Level | Token | Gates |
|---|---|---|
| **admin** | `PHERIX_CP_ADMIN_KEY` | org *creation* only |
| **org key** | `pk_…` minted per org | everything else, scoped to the key's org |

Org keys are stored **only as sha256 hashes** — the plaintext is returned exactly
once at creation and never again. Every org-scoped query is filtered by the
resolved `org_id`: tenant isolation is by construction.

Identity is **opaque references**, not an identity provider: users / owners /
approvers are strings (email / group / SSO subject) the enterprise's own SSO
resolves. We only carry the reference.

All routes are under `/api/v1`.

## Identity (surface 1)

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| POST | `/orgs` | admin | `{name}` → `{org_id, name, api_key}` *(key shown once)* |
| POST | `/keys` | org | `{name?}` → `{key_id, name, api_key}` *(shown once)* |
| GET | `/keys` | org | → `[{key_id, name, created_at, last_used_at, revoked}]` |
| DELETE | `/keys/{key_id}` | org | → `{revoked}` (404 if absent) |
| POST | `/users` | org | `{ref, role?}` → `{user_id, ref, role, created_at}` |
| GET | `/users` | org | → `[UserInfo]` |

## Fleet registry (surface 2)

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| POST | `/agents` | org | `{name, owner}` → `{agent_id, name, owner, created_at}` |
| GET | `/agents` | org | → `[AgentInfo]` |
| GET | `/agents/{agent_id}` | org | → `AgentInfo` (404 if absent) |

`owner` is the opaque reference for the accountable party.

## Policy distribution (surface 3)

The policy *primitives* live in the OSS `pherix.governance`. The control plane
stores, versions, and serves the spec JSON (opaque to it); the SDK pulls + enforces
at the edge.

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| POST | `/policies` | org | `{name}` → `{policy_id, name, created_at}` |
| GET | `/policies` | org | → `[PolicyInfo]` |
| POST | `/policies/{policy_id}/versions` | org | `{spec}` → `{policy_id, version, created_at}` |
| GET | `/policies/{policy_id}/versions` | org | → `[{policy_id, version, created_at}]` |
| PUT | `/agents/{agent_id}/policy` | org | `{policy_id, version?}` → `{agent_id, policy_id, version}` |
| GET | `/agents/{agent_id}/policy` | org | → `{policy_id, version, spec}` — **the SDK pull** |

Versions are 1-based and monotonic per policy. Assigning with `version: null` pins
the agent to **latest**, so pushing a new version propagates to every `latest`
agent on its next pull. Pinning a number freezes that agent.

## Journal ingest (surface 4)

Opt-in, append-only, batched, **idempotent** on primary keys (re-shipping a row is
a no-op). The SDK ships new rows past its local cursor; PII is redacted
**client-side** before anything leaves the agent's host.

```
POST /ingest   (org auth)
{
  "agent_id": "agt_…",          # must belong to the org (404 otherwise)
  "host": "worker-3",           # provenance
  "transactions": [
    {"txn_id", "state", "session_id"?, "created_at"?, "updated_at"?,
     "dry_run"?, "client_id"?}
  ],
  "effects": [
    {"txn_id", "idx", "effect_id", "tool", "resource", "reversible",
     "status", "args"?, "result"?, "ts"?}      # args is a JSON string, redacted
  ],
  "verdicts": [
    {"txn_id", "effect_index", "seq_in_txn", "phase", "allow",
     "kind"?, "rule_name"?, "reason"?}
  ]
}
→ {accepted, skipped, cursor}    # cursor = org high-water seq

GET /ingest/cursor   (org auth)  → {cursor}
```

These shapes mirror the audit-journal rows (`pherix/core/audit.py`) plus the
multi-host additions: `session_id` (timeline grouping) and `host` (provenance).

## Cross-host audit search (surface 5)

Every agent's journal lands in one retained store, so a single query spans the
whole fleet — not one agent's local SQLite. Results are org-scoped and ordered by
ingest seq (newest first).

| Method | Path | Query params |
|---|---|---|
| GET | `/audit/effects` | `agent_id, session_id, tool, status, resource, since, until, limit` |
| GET | `/audit/verdicts` | `allow` (False ⇒ every denied effect), `agent_id, rule_name, limit` |
| GET | `/audit/sessions/{session_id}` | — (404 if no ingested transactions) |

`/audit/effects` and `/audit/verdicts` return `{count, results}`. The session
timeline returns `{session_id, transactions, effects}` ordered by seq — the
observational "session replay" the dashboard renders (re-execution replay already
lives in the SDK).

## Out of scope (v2 — pull-driven, not built)

Metering / billing (retention bands), governed-memory-as-a-service, and the
cross-host arbiter for distributed locks + hard cross-process budgets (#8 / #10).
