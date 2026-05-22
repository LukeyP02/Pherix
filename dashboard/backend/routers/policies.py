"""Policy distribution surface (surface 3).

Define a policy centrally and version it; assign a version (or "latest") to an
agent; the SDK *pulls* the resolved spec and enforces it at the edge. The policy
*primitives* live in the OSS ``pherix.governance`` — the control plane stores,
versions, and serves the JSON, it does not interpret it. The spec is opaque here.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from dashboard.backend.auth import OrgDep
from dashboard.backend.models import (
    AddPolicyVersion,
    AssignPolicy,
    CreatePolicy,
    PolicyInfo,
    PolicyVersionInfo,
    ResolvedPolicy,
)

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.post("/policies", response_model=PolicyInfo)
def create_policy(body: CreatePolicy, request: Request, org_id: str = OrgDep) -> PolicyInfo:
    return PolicyInfo(**_store(request).create_policy(org_id, body.name))


@router.get("/policies", response_model=list[PolicyInfo])
def list_policies(request: Request, org_id: str = OrgDep) -> list[PolicyInfo]:
    return [PolicyInfo(**p) for p in _store(request).list_policies(org_id)]


@router.post("/policies/{policy_id}/versions", response_model=PolicyVersionInfo)
def add_version(
    policy_id: str, body: AddPolicyVersion, request: Request, org_id: str = OrgDep
) -> PolicyVersionInfo:
    store = _store(request)
    if store.get_policy(org_id, policy_id) is None:
        raise HTTPException(status_code=404, detail="policy not found")
    rec = store.add_policy_version(org_id, policy_id, json.dumps(body.spec))
    return PolicyVersionInfo(
        policy_id=policy_id, version=rec["version"], created_at=rec["created_at"]
    )


@router.get("/policies/{policy_id}/versions", response_model=list[PolicyVersionInfo])
def list_versions(
    policy_id: str, request: Request, org_id: str = OrgDep
) -> list[PolicyVersionInfo]:
    store = _store(request)
    if store.get_policy(org_id, policy_id) is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return [
        PolicyVersionInfo(policy_id=policy_id, version=v["version"], created_at=v["created_at"])
        for v in store.list_policy_versions(org_id, policy_id)
    ]


@router.put("/agents/{agent_id}/policy")
def assign_policy(
    agent_id: str, body: AssignPolicy, request: Request, org_id: str = OrgDep
) -> dict:
    """Bind an agent to a policy version (or to 'latest' when version is null)."""
    store = _store(request)
    if store.get_agent(org_id, agent_id) is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if store.get_policy_version(org_id, body.policy_id, body.version) is None:
        raise HTTPException(status_code=404, detail="policy version not found")
    store.assign_policy(org_id, agent_id, body.policy_id, body.version)
    return {"agent_id": agent_id, "policy_id": body.policy_id, "version": body.version}


@router.get("/agents/{agent_id}/policy", response_model=ResolvedPolicy)
def pull_policy(agent_id: str, request: Request, org_id: str = OrgDep) -> ResolvedPolicy:
    """The distribution pull: the SDK fetches its agent's resolved policy spec.

    A null pinned version resolves to the latest at pull time, so pushing a new
    version propagates to every agent on 'latest' the next time it pulls.
    """
    store = _store(request)
    if store.get_agent(org_id, agent_id) is None:
        raise HTTPException(status_code=404, detail="agent not found")
    assignment = store.get_assignment(org_id, agent_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="no policy assigned to agent")
    pv = store.get_policy_version(org_id, assignment["policy_id"], assignment["version"])
    if pv is None:
        raise HTTPException(status_code=404, detail="assigned policy version missing")
    return ResolvedPolicy(
        policy_id=assignment["policy_id"],
        version=pv["version"],
        spec=json.loads(pv["spec"]),
    )
