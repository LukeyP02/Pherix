"""FastAPI app — static site + the real-engine policy endpoints.

The headline endpoint is ``POST /api/preview``: it folds a candidate policy over
a sample journal using the genuine engine (``pherix.governance.preview``), so the
verdicts the governance page renders are the verdicts the runtime produces — not
a JS mirror that can drift. ``/api/templates`` and ``/api/policy/python`` expose
the starter catalog and the codegen the same way, for SDK/automation callers.

Route order matters: the ``/api/*`` routes are declared before the catch-all
static mount at ``/`` so they win; everything else falls through to ``site/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pherix.governance import STARTER_TEMPLATES, preview, to_python

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"

app = FastAPI(
    title="Pherix",
    description="Transactional guardrails for autonomous agents — public site + policy API.",
    version="0.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# -- request shapes ----------------------------------------------------------


class PreviewRequest(BaseModel):
    """A candidate policy + the sample journal to fold it over."""

    spec: dict[str, Any] = Field(..., description="PolicySpec JSON (the governance builder's export shape).")
    scenario: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered sample effects: {tool, args, reversible, compensator, resource?}.",
    )
    world: list[dict[str, Any]] | None = Field(
        default=None,
        description="Sample world-state rows {resource, key, value} the rules read via ctx.read.",
    )


class ExportRequest(BaseModel):
    spec: dict[str, Any] = Field(..., description="PolicySpec JSON to render as a runnable module.")


# -- endpoints ---------------------------------------------------------------


@app.post("/api/preview")
def api_preview(req: PreviewRequest) -> dict[str, Any]:
    """Run the real ``pherix.governance.preview`` and return the verdicts.

    The response shape matches what ``site/governance.js`` renders directly:
    ``{counts, is_clean, rows:[{index, tool, disposition, reasons}]}``.
    """
    try:
        result = preview(req.spec, req.scenario, world=req.world)
    except Exception as exc:  # bad spec / unknown template — let the UI fall back
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {
        "counts": result.counts,
        "is_clean": result.is_clean,
        "rows": [
            {
                "index": r.index,
                "tool": r.tool,
                "disposition": r.disposition,
                "reasons": r.reasons,
            }
            for r in result.rows
        ],
    }


@app.get("/api/templates")
def api_templates() -> list[dict[str, Any]]:
    """The vetted starter policies, as JSON the builder can load."""
    return [t.to_dict() for t in STARTER_TEMPLATES]


@app.post("/api/policy/python")
def api_policy_python(req: ExportRequest) -> dict[str, Any]:
    """Render the spec as a runnable Python module (engine-faithful codegen)."""
    try:
        return {"code": to_python(req.spec)}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "pherix"}


# -- static site (catch-all; must mount last) --------------------------------

app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="site")
