"""Cross-host arbiter (control-plane v2) — the central authority for distributed
locks and hard budgets.

Two single-host engine guarantees, lifted to span hosts:

- **Locks (#8 across hosts).** The single-host write-intent table lets a Serialize
  commit wait on a conflicting in-flight writer in the *same* process group. The
  arbiter is the same idea with a central registry: a commit about to write a set
  of keys asks to lock them; if any carries a live lock held by another agent, it
  is refused with the conflicting holders, and the caller waits / retries / aborts.
  A TTL reclaims a dead holder's locks, the cross-host analogue of intent staleness.

- **Budgets (#10 hard, across hosts).** The longitudinal envelope caps spend
  durably on one host. The arbiter holds one central counter per budget, and the
  check-and-increment is atomic, so concurrent hosts cannot collectively overspend.

Org-scoped throughout — an org's locks and budgets are invisible to every other.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from dashboard.backend.auth import OrgDep
from dashboard.backend.models import (
    BudgetSpend,
    BudgetState,
    LockRequest,
    LockResult,
    ReleaseResult,
)

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.post("/arbiter/locks", response_model=LockResult)
def acquire_locks(body: LockRequest, request: Request, org_id: str = OrgDep) -> LockResult:
    res = _store(request).acquire_locks(
        org_id, body.resource, body.keys, body.holder, body.ttl
    )
    return LockResult(**res)


@router.delete("/arbiter/locks/{holder}", response_model=ReleaseResult)
def release_locks(holder: str, request: Request, org_id: str = OrgDep) -> ReleaseResult:
    return ReleaseResult(released=_store(request).release_locks(org_id, holder))


@router.post("/arbiter/budget/{budget_key}/spend", response_model=BudgetState)
def spend_budget(
    budget_key: str, body: BudgetSpend, request: Request, org_id: str = OrgDep
) -> BudgetState:
    try:
        res = _store(request).spend_budget(org_id, budget_key, body.amount, body.cap)
    except ValueError as exc:
        # First use of a budget without a cap — the caller must set the ceiling.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BudgetState(**res)
