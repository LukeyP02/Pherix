"""Governed-memory-as-a-service (control-plane v2).

The MemoryAdapter's namespaced key/value memory — remember / recall / forget —
served over the wire and made multi-tenant + host-shared. Governed memory was
never a new axis (it is an adapter + a policy); here it is that adapter exposed
as an org-scoped HTTP surface so agents across many hosts share one memory under
one policy. Values are content-addressed (version = sha256), so a recaller can
tell that another host rewrote a key. Every route is org-scoped — one org never
sees another's namespaces.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from dashboard.backend.auth import OrgDep
from dashboard.backend.models import MemoryKey, MemoryRef, MemoryValue, MemoryWrite

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.post("/memory/{namespace}/remember", response_model=MemoryRef)
def remember(
    namespace: str, body: MemoryWrite, request: Request, org_id: str = OrgDep
) -> MemoryRef:
    res = _store(request).mem_remember(org_id, namespace, body.key, body.value)
    return MemoryRef(**res)


@router.get("/memory/{namespace}/recall", response_model=MemoryValue)
def recall(
    namespace: str, key: str, request: Request, org_id: str = OrgDep
) -> MemoryValue:
    row = _store(request).mem_recall(org_id, namespace, key)
    if row is None:
        raise HTTPException(status_code=404, detail="memory key not found")
    return MemoryValue(key=key, value=row["value"], version=row["version"])


@router.post("/memory/{namespace}/forget")
def forget(
    namespace: str, body: MemoryWrite, request: Request, org_id: str = OrgDep
) -> dict:
    forgotten = _store(request).mem_forget(org_id, namespace, body.key)
    return {"forgotten": forgotten}


@router.get("/memory/{namespace}", response_model=list[MemoryKey])
def list_keys(namespace: str, request: Request, org_id: str = OrgDep) -> list[MemoryKey]:
    return [MemoryKey(**r) for r in _store(request).mem_list(org_id, namespace)]
