"""Fleet registry surface (surface 2).

Agents are first-class entities with an ``owner`` (an opaque SSO reference) — the
accountability surface a fleet needs: every agent traces to a responsible party.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from dashboard.backend.auth import OrgDep
from dashboard.backend.models import AgentInfo, CreateAgent

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.post("/agents", response_model=AgentInfo)
def create_agent(body: CreateAgent, request: Request, org_id: str = OrgDep) -> AgentInfo:
    return AgentInfo(**_store(request).create_agent(org_id, body.name, body.owner))


@router.get("/agents", response_model=list[AgentInfo])
def list_agents(request: Request, org_id: str = OrgDep) -> list[AgentInfo]:
    return [AgentInfo(**a) for a in _store(request).list_agents(org_id)]


@router.get("/agents/{agent_id}", response_model=AgentInfo)
def get_agent(agent_id: str, request: Request, org_id: str = OrgDep) -> AgentInfo:
    agent = _store(request).get_agent(org_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return AgentInfo(**agent)
