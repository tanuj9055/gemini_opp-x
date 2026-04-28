"""
GeM Procurement Audit Service – FastAPI application factory.

Run with:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_cfg import logger
from app.routers import hsn
from app.routers import test_routes
from app.worker.extraction_consumer import start_extraction_worker
from app.worker.hsn_consumer import start_hsn_worker
from app.worker.analysis_consumer import start_analysis_worker
from app.worker.classification_consumer import start_classification_worker
from app.worker.evaluation_consumer import start_evaluation_worker
from app.worker.filter_consumer import start_filter_worker

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

    # ── Start RabbitMQ consumers in the background ─────────────
    extraction_worker_task = asyncio.create_task(
        start_extraction_worker(settings.rabbitmq_url),
        name="rabbitmq-extraction-worker",
    )
    analysis_worker_task = asyncio.create_task(
        start_analysis_worker(settings.rabbitmq_url),
        name="rabbitmq-analysis-worker",
    )
    classification_worker_task = asyncio.create_task(
        start_classification_worker(settings.rabbitmq_url),
        name="rabbitmq-classification-worker",
    )
    evaluation_worker_task = asyncio.create_task(
        start_evaluation_worker(settings.rabbitmq_url),
        name="rabbitmq-evaluation-worker",
    )
    filter_worker_task = asyncio.create_task(
        start_filter_worker(settings.rabbitmq_url),
        name="rabbitmq-filter-worker",
    )

    yield

    for task in (extraction_worker_task, analysis_worker_task, classification_worker_task, evaluation_worker_task, filter_worker_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    _log.info("Shutting down GeM Audit Service")


# ────────────────────────────────────────────────────────
# App instance
# ────────────────────────────────────────────────────────

app = FastAPI(
    title="GeM Multi-Agent Audit Service",
    description=(
        "Automated auditing of Government e-Marketplace (GeM) procurement "
        "documents using a modular multi-agent architecture powered by Gemini 1.5 Pro.  \n\n"
        "**Agent 1: Rule Extraction** — Identifies structured eligibility criteria from bid documents.  \n"
        "**Agent 2: Bid Analysis** — Generates high-level summaries and highlights for vendors.  \n"
        "**Agent 3: Classification** — Maps criteria to vendor profile data fields.  \n"
        "**Agent 4: Evaluation** — Performs automated pass/fail assessment.  \n"
        "**Agent 1b: Verifiable Filter** — Separates checkable conditions from narrative ones.  \n\n"
        "The service operates as an asynchronous pipeline via RabbitMQ, with synchronous `/test` endpoints and `/generate-hsn` available for development and direct integration."
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
app.include_router(hsn.router)
app.include_router(test_routes.router)

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
