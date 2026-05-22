"""Identity surface — orgs, API keys, users (surface 1).

Org *creation* is admin-gated and returns the org's first API key once. Everything
else is org-scoped: the key resolves to an ``org_id`` and all queries filter on it.
Users are opaque SSO references we merely carry — not an identity provider.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from dashboard.backend.auth import (
    AdminDep,
    OrgDep,
    generate_key,
    hash_key,
    require_org,
)
from dashboard.backend.models import (
    CreateKey,
    CreateOrg,
    CreateUser,
    KeyCreated,
    KeyInfo,
    OrgCreated,
    UserInfo,
)

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


@router.post("/orgs", response_model=OrgCreated, dependencies=[AdminDep])
def create_org(body: CreateOrg, request: Request) -> OrgCreated:
    """Create a tenant and mint its first API key (shown once)."""
    store = _store(request)
    org = store.create_org(body.name)
    plaintext = generate_key()
    store.insert_key(org["org_id"], hash_key(plaintext), name="initial")
    return OrgCreated(org_id=org["org_id"], name=org["name"], api_key=plaintext)


@router.post("/keys", response_model=KeyCreated)
def create_key(body: CreateKey, request: Request, org_id: str = OrgDep) -> KeyCreated:
    """Mint an additional key for the calling org."""
    plaintext = generate_key()
    rec = _store(request).insert_key(org_id, hash_key(plaintext), body.name)
    return KeyCreated(key_id=rec["key_id"], name=rec["name"], api_key=plaintext)


@router.get("/keys", response_model=list[KeyInfo])
def list_keys(request: Request, org_id: str = OrgDep) -> list[KeyInfo]:
    return [
        KeyInfo(
            key_id=k["key_id"],
            name=k["name"],
            created_at=k["created_at"],
            last_used_at=k["last_used_at"],
            revoked=bool(k["revoked"]),
        )
        for k in _store(request).list_keys(org_id)
    ]


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, request: Request, org_id: str = OrgDep) -> dict:
    if not _store(request).revoke_key(org_id, key_id):
        raise HTTPException(status_code=404, detail="key not found")
    return {"revoked": key_id}


@router.post("/users", response_model=UserInfo)
def create_user(body: CreateUser, request: Request, org_id: str = OrgDep) -> UserInfo:
    return UserInfo(**_store(request).create_user(org_id, body.ref, body.role))


@router.get("/users", response_model=list[UserInfo])
def list_users(request: Request, org_id: str = OrgDep) -> list[UserInfo]:
    return [UserInfo(**u) for u in _store(request).list_users(org_id)]
