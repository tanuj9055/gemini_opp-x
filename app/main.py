"""
GeM Procurement Audit Service – FastAPI application factory.

Run with:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_cfg import logger
from app.routers import bid, vendor

_log = logger.getChild("main")


# ────────────────────────────────────────────────────────
# Lifespan
# ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hook."""
    settings = get_settings()
    _log.info(
        "Starting GeM Audit Service [env=%s, model=%s]",
        settings.app_env,
        settings.gemini_model,
    )
    yield
    _log.info("Shutting down GeM Audit Service")


# ────────────────────────────────────────────────────────
# App instance
# ────────────────────────────────────────────────────────

app = FastAPI(
    title="GeM Procurement Audit Service",
    description=(
        "Automated auditing of Government e-Marketplace (GeM) procurement "
        "documents using Gemini 1.5 Pro multimodal AI.  \n\n"
        "**Stage 1** — `/analyze-bid` extracts structured eligibility criteria "
        "from a bid PDF.  \n"
        "**Stage 2** — `/evaluate-vendor` cross-references vendor documents "
        "against bid criteria to compute an eligibility score and recommendation."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS (permissive for dev; tighten for production) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────
app.include_router(bid.router)
app.include_router(vendor.router)


# ── Health check ─────────────────────────────────────────

@app.get("/health", tags=["Health"], summary="Service health check")
async def health_check():
    """Returns service readiness status."""
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.app_env,
        "model": settings.gemini_model,
    }
