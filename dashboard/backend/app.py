"""FastAPI app for the Pherix control plane.

``create_app(store, admin_key)`` is the factory — tests inject an in-memory store
and a known admin key directly. The module-level ``app`` is built from environment
(``PHERIX_CP_DB`` for the SQLite path, ``PHERIX_CP_ADMIN_KEY`` for the bootstrap
secret) for running the service, mirroring the FastAPI precedent in ``server/app.py``.

The control plane only ever *consumes* the substrate; it imports nothing from
``pherix/`` and reaches into no engine.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from dashboard.backend.db import Store
from dashboard.backend.routers import (
    agents,
    arbiter,
    ingest,
    memory,
    metering,
    orgs,
    policies,
    search,
)


def create_app(store: Store, admin_key: str) -> FastAPI:
    app = FastAPI(
        title="Pherix Control Plane",
        description=(
            "Multi-tenant commercial layer: orgs/users/keys, the agent registry, "
            "versioned policy distribution, opt-in journal ingest, cross-host "
            "audit search, governed-memory-as-a-service, the cross-host arbiter "
            "(distributed locks + hard budgets), and minimal usage metering. "
            "Consumes the substrate; never reaches into the engine."
        ),
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.store = store
    app.state.admin_key = admin_key

    app.include_router(orgs.router, prefix="/api/v1", tags=["identity"])
    app.include_router(agents.router, prefix="/api/v1", tags=["fleet"])
    app.include_router(policies.router, prefix="/api/v1", tags=["policy"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["journal"])
    app.include_router(search.router, prefix="/api/v1", tags=["audit"])
    app.include_router(memory.router, prefix="/api/v1", tags=["memory"])
    app.include_router(arbiter.router, prefix="/api/v1", tags=["arbiter"])
    app.include_router(metering.router, prefix="/api/v1", tags=["metering"])

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "pherix-control-plane"}

    return app


def _from_env() -> FastAPI:
    db_path = os.environ.get("PHERIX_CP_DB", "pherix_control_plane.db")
    admin_key = os.environ.get("PHERIX_CP_ADMIN_KEY", "")
    return create_app(Store(db_path), admin_key)


_env_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Build the env-configured app lazily on first access to ``app``.

    Lazy so that merely *importing* this module (as the tests do, to reach
    ``create_app``) never opens a database or touches the filesystem. ``uvicorn
    dashboard.backend.app:app`` resolves ``app`` through here at server start.
    """
    global _env_app
    if name == "app":
        if _env_app is None:
            _env_app = _from_env()
        return _env_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
