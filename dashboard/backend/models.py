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
    encrypted: bool = Field(
        default=False,
        description="True when effect args/result are ciphertext under the "
        "customer's key — the control plane stores them but cannot read them.",
    )


class IngestResult(BaseModel):
    accepted: int
    skipped: int = Field(..., description="Rows already present (idempotent re-ship).")
    cursor: int = Field(..., description="Org high-water seq after this batch.")


# -- control-plane v2: governed memory over the wire -------------------------


class MemoryWrite(BaseModel):
    key: str
    value: str = Field(..., description="Opaque value; the customer encrypts before sending if sensitive.")


class MemoryValue(BaseModel):
    key: str
    value: str
    version: str = Field(..., description="sha256 of the value — content-addressed, detects concurrent rewrites.")


class MemoryRef(BaseModel):
    namespace: str
    key: str
    version: str


class MemoryKey(BaseModel):
    key: str
    version: str


# -- control-plane v2: cross-host arbiter ------------------------------------


class LockRequest(BaseModel):
    resource: str = Field(..., description="Resource class the keys belong to (e.g. 'sql').")
    keys: list[str] = Field(..., description="Keys to lock, all-or-nothing.")
    holder: str = Field(..., description="Opaque holder id (txn_id or agent+txn) released as a unit.")
    ttl: float = Field(default=60.0, description="Seconds before a dead holder's lock is reclaimable.")


class LockConflict(BaseModel):
    key: str
    holder: str


class LockResult(BaseModel):
    acquired: bool
    conflicts: list[LockConflict] = Field(default_factory=list)


class ReleaseResult(BaseModel):
    released: int


class BudgetSpend(BaseModel):
    amount: float = Field(..., description="How much to spend against the budget.")
    cap: float | None = Field(
        default=None,
        description="Ceiling; required on first use of a budget_key, optional after.",
    )


class BudgetState(BaseModel):
    allowed: bool
    spent: float
    cap: float
    remaining: float


# -- control-plane v2: minimal metering --------------------------------------


class UsageReport(BaseModel):
    org_id: str
    usage: dict[str, int] = Field(default_factory=dict, description="metric -> count. Record only; no pricing.")
