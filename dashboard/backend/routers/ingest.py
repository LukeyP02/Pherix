"""Journal ingestion surface (surface 4) — the retained-journal product surface.

The SDK pushes new journal rows past its local cursor: append-only, batched (so it
never blocks the agent loop), and idempotent on primary keys (re-shipping a row is a
no-op). PII is redacted *client-side* before anything leaves the agent's host — this
endpoint stores what arrives and never sees raw payloads. The whole channel is
opt-in; an agent that never calls it ships us nothing and runs fully offline.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from dashboard.backend.auth import OrgDep
from dashboard.backend.models import IngestBatch, IngestResult

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.post("/ingest", response_model=IngestResult)
def ingest(body: IngestBatch, request: Request, org_id: str = OrgDep) -> IngestResult:
    store = _store(request)
    if store.get_agent(org_id, body.agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent_id for org")
    result = store.ingest(
        org_id,
        body.agent_id,
        body.host,
        [t.model_dump() for t in body.transactions],
        [e.model_dump() for e in body.effects],
        [v.model_dump() for v in body.verdicts],
    )
    return IngestResult(**result)


@router.get("/ingest/cursor")
def cursor(request: Request, org_id: str = OrgDep) -> dict:
    """The org's high-water seq — the SDK reconciles its local cursor against this."""
    return {"cursor": _store(request).high_water(org_id)}
