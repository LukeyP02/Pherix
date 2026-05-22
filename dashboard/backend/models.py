"""Pydantic request/response shapes — the wire contract for the control plane.

These are the JSON shapes the operator's frontend/UI and the SDK build against.
The full contract (routes + these shapes) is also served live as OpenAPI at
``/api/docs`` and summarised in ``dashboard/backend/API.md``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# -- identity ----------------------------------------------------------------


class CreateOrg(BaseModel):
    name: str = Field(..., description="Human-readable org name.")


class OrgCreated(BaseModel):
    org_id: str
    name: str
    api_key: str = Field(..., description="Initial org API key — shown once, never again.")


class CreateKey(BaseModel):
    name: str | None = Field(default=None, description="Label for the key (e.g. 'ci', 'prod-fleet').")


class KeyCreated(BaseModel):
    key_id: str
    name: str | None
    api_key: str = Field(..., description="Plaintext key — shown once, never again.")


class KeyInfo(BaseModel):
    key_id: str
    name: str | None
    created_at: str
    last_used_at: str | None
    revoked: bool


class CreateUser(BaseModel):
    ref: str = Field(..., description="Opaque SSO reference (email / group / subject). Not resolved here.")
    role: str | None = Field(default=None, description="Opaque label the enterprise assigns.")


class UserInfo(BaseModel):
    user_id: str
    ref: str
    role: str | None
    created_at: str


# -- fleet -------------------------------------------------------------------


class CreateAgent(BaseModel):
    name: str = Field(..., description="Agent display name.")
    owner: str = Field(..., description="Opaque SSO reference for the accountable party.")


class AgentInfo(BaseModel):
    agent_id: str
    name: str
    owner: str
    created_at: str


# -- policy ------------------------------------------------------------------


class CreatePolicy(BaseModel):
    name: str = Field(..., description="Logical policy name; versions are appended under it.")


class PolicyInfo(BaseModel):
    policy_id: str
    name: str
    created_at: str


class AddPolicyVersion(BaseModel):
    spec: dict[str, Any] = Field(
        ..., description="PolicySpec JSON (the OSS governance builder's export). Opaque here."
    )


class PolicyVersionInfo(BaseModel):
    policy_id: str
    version: int
    created_at: str


class AssignPolicy(BaseModel):
    policy_id: str
    version: int | None = Field(
        default=None, description="Pin a version, or null to always pull the latest."
    )


class ResolvedPolicy(BaseModel):
    """What an agent pulls from the distribution endpoint to enforce at the edge."""

    policy_id: str
    version: int
    spec: dict[str, Any]


# -- journal ingest ----------------------------------------------------------


class IngestTransaction(BaseModel):
    txn_id: str
    state: str
    session_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    dry_run: bool = False
    client_id: str | None = None


class IngestEffect(BaseModel):
    txn_id: str
    idx: int
    effect_id: str
    tool: str
    resource: str
    reversible: bool = False
    status: str
    args: str | None = Field(default=None, description="JSON string; PII redacted client-side.")
    result: str | None = None
    ts: str | None = None


class IngestVerdict(BaseModel):
    txn_id: str
    effect_index: int
    seq_in_txn: int
    phase: str
    allow: bool
    kind: str = "rule"
    rule_name: str | None = None
    reason: str | None = None


class IngestBatch(BaseModel):
    """An opt-in, append-only, batched journal push. The SDK sends new rows past
    its local cursor; the endpoint is idempotent on primary keys."""

    agent_id: str
    host: str | None = None
    transactions: list[IngestTransaction] = Field(default_factory=list)
    effects: list[IngestEffect] = Field(default_factory=list)
    verdicts: list[IngestVerdict] = Field(default_factory=list)


class IngestResult(BaseModel):
    accepted: int
    skipped: int = Field(..., description="Rows already present (idempotent re-ship).")
    cursor: int = Field(..., description="Org high-water seq after this batch.")
