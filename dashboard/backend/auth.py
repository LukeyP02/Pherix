"""Authentication — API keys for orgs, an admin key for bootstrap.

Two trust levels, both bearer tokens in the ``Authorization`` header:

* **admin** — a single operator-held secret (env ``PHERIX_CP_ADMIN_KEY``) that gates
  org *creation* only. Everything else is org-scoped.
* **org key** — minted per org. The plaintext (``pk_…``) is shown exactly once at
  creation; we persist only its sha256 hash, so a store dump never leaks a usable
  key. Every org-scoped request resolves its key hash to an ``org_id`` and all
  queries are filtered by that id — tenant isolation by construction.

We deliberately do **not** build an identity provider. Users/owners/approvers are
*opaque references* (email, group, SSO subject) that the enterprise's own SSO
resolves; we only carry the reference.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, Request


def generate_key() -> str:
    """A fresh org API key. ``pk_`` prefix + 32 bytes of url-safe entropy."""
    return f"pk_{secrets.token_urlsafe(32)}"


def hash_key(plaintext: str) -> str:
    """sha256 hex of a key. Keys are high-entropy random tokens, so a plain
    cryptographic hash (no salt/KDF) is the right primitive — there is no
    low-entropy password to slow brute-force against."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """Gate an endpoint behind the operator's admin key."""
    admin_key = request.app.state.admin_key
    presented = _bearer(authorization)
    # constant-time compare; both sides hashed so length never leaks
    if not admin_key or presented is None or not secrets.compare_digest(
        hash_key(presented), hash_key(admin_key)
    ):
        raise HTTPException(status_code=401, detail="admin credentials required")


def require_org(
    request: Request, authorization: str | None = Header(default=None)
) -> str:
    """Resolve a presented org API key to its ``org_id``; 401 if unknown/revoked.

    The returned ``org_id`` is the tenant scope every downstream query filters on.
    """
    presented = _bearer(authorization)
    if presented is None:
        raise HTTPException(status_code=401, detail="API key required")
    resolved = request.app.state.store.resolve_key(hash_key(presented))
    if resolved is None:
        raise HTTPException(status_code=401, detail="invalid or revoked API key")
    return resolved["org_id"]


# Convenience aliases for router signatures.
AdminDep = Depends(require_admin)
OrgDep = Depends(require_org)
