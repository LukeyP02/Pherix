"""Minimal metering (control-plane v2) — usage record, no pricing.

Records governed usage so the question "how much is this org using" has an
answer; it deliberately stops there. Pricing and billing cannot be designed
without a design partner, so they are out of scope — usage record now,
monetisation later. The ingest path increments counters in the same transaction
as the rows land; this surface just reads them back, org-scoped.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from dashboard.backend.auth import OrgDep
from dashboard.backend.models import UsageReport

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.get("/metering/usage", response_model=UsageReport)
def usage(request: Request, org_id: str = OrgDep) -> UsageReport:
    return UsageReport(org_id=org_id, usage=_store(request).get_usage(org_id))
