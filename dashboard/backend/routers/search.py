"""Cross-host audit search surface (surface 5).

Because every agent's journal lands in one retained store, a single query spans the
whole fleet — not one agent's local SQLite. Effects can be filtered by tool, status,
resource, agent, session, or time window; verdicts answer "every denied effect"
(allow=False); and a session_id rolls up into an ordered timeline — the
observational session replay the dashboard renders (re-execution replay already
lives in the SDK).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from dashboard.backend.auth import OrgDep

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.get("/audit/effects")
def search_effects(
    request: Request,
    org_id: str = OrgDep,
    agent_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    status: str | None = Query(default=None),
    resource: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO timestamp lower bound (inclusive)."),
    until: str | None = Query(default=None, description="ISO timestamp upper bound (inclusive)."),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    rows = _store(request).search_effects(
        org_id,
        agent_id=agent_id,
        session_id=session_id,
        tool=tool,
        status=status,
        resource=resource,
        since=since,
        until=until,
        limit=limit,
    )
    return {"count": len(rows), "results": rows}


@router.get("/audit/verdicts")
def search_verdicts(
    request: Request,
    org_id: str = OrgDep,
    allow: bool | None = Query(default=None, description="False surfaces every denied effect."),
    agent_id: str | None = Query(default=None),
    rule_name: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    rows = _store(request).search_verdicts(
        org_id, allow=allow, agent_id=agent_id, rule_name=rule_name, limit=limit
    )
    return {"count": len(rows), "results": rows}


@router.get("/audit/sessions/{session_id}")
def session_timeline(session_id: str, request: Request, org_id: str = OrgDep) -> dict:
    timeline = _store(request).session_timeline(org_id, session_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="no ingested transactions for session")
    return timeline
