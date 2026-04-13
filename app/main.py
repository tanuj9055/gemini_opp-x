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
from app.routers import bid, bid_package, hsn, orchestrator, vendor
from app.routers import test_routes
from app.services.rabbitmq_consumer import start_consumer
from app.worker.consumer import start_worker
from app.worker.extraction_consumer import start_extraction_worker
from app.worker.hsn_consumer import start_hsn_worker
from app.worker.pdf_consumer import start_pdf_worker
from app.worker.analysis_consumer import start_analysis_worker
from app.worker.classification_consumer import start_classification_worker
from app.worker.evaluation_consumer import start_evaluation_worker

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
    consumer_task = asyncio.create_task(
        start_consumer(settings.rabbitmq_url),
        name="rabbitmq-consumer",
    )
    worker_task = asyncio.create_task(
        start_worker(settings.rabbitmq_url),
        name="rabbitmq-ai-worker",
    )
    pdf_worker_task = asyncio.create_task(
        start_pdf_worker(settings.rabbitmq_url),
        name="rabbitmq-pdf-worker",
    )
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

    yield

    for task in (consumer_task, worker_task, pdf_worker_task, extraction_worker_task, analysis_worker_task, classification_worker_task, evaluation_worker_task):
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
    title="GeM Procurement Audit Service",
    description=(
        "Automated auditing of Government e-Marketplace (GeM) procurement "
        "documents using Gemini 1.5 Pro multimodal AI.  \n\n"
        "**Stage 1** — `/analyze-bid` extracts structured eligibility criteria "
        "from a bid PDF.  \n"
        "**Stage 2** — `/evaluate-vendor` cross-references vendor documents "
        "against bid criteria to compute an eligibility score and recommendation.  \n"
        "**Pipeline** — `/process-bid-evaluation` orchestrates the full pipeline: "
        "fetch bid from S3, extract criteria, fetch vendor docs, evaluate, and return results."
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
app.include_router(orchestrator.router)
app.include_router(hsn.router)
app.include_router(bid_package.router)
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
