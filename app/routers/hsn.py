"""
HSN Code Generation router.

Provides an HTTP endpoint for direct HSN generation (without RabbitMQ)
and a publish endpoint that pushes a job onto the ``hsn_generation_jobs``
queue for async processing.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.logging_cfg import logger
from app.services.hsn_generator import generate_hsn_codes

_log = logger.getChild("router.hsn")

router = APIRouter(tags=["HSN"])


# ────────────────────────────────────────────────────────
# Request / Response models
# ────────────────────────────────────────────────────────

class HsnBidItem(BaseModel):
    bid_id: str = Field(..., description="Unique bid identifier")
    item: str = Field(..., description="Item description for HSN classification")


class HsnRequest(BaseModel):
    bids: List[HsnBidItem] = Field(
        ..., min_length=1, description="List of bid items to classify"
    )


class HsnResultItem(BaseModel):
    bid_id: Optional[str] = None
    hsn: Optional[str] = None
    confidence: Optional[str] = None
    reasoning: Optional[str] = None


class HsnMetaData(BaseModel):
    total_bids_processed: int = 0
    model_used: str = ""
    execution_time_ms: int = 0
    prompt_length: Optional[int] = None
    response_length: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None


class HsnResponse(BaseModel):
    status: str
    meta_data: HsnMetaData
    data: dict = {}
    error_code: str = ""
    error_messages: List[str] = []


# ────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────

@router.post(
    "/generate-hsn",
    response_model=HsnResponse,
    summary="Generate HSN codes for bid items",
    description=(
        "Accepts a list of bid items and returns HSN (Harmonized System of "
        "Nomenclature) codes using Gemini AI.  For async processing via "
        "RabbitMQ, publish to the ``hsn_generation_jobs`` queue instead."
    ),
)
async def generate_hsn(request: HsnRequest):
    """Synchronous (HTTP) HSN generation — returns results directly."""
    _log.info("POST /generate-hsn – %d bids", len(request.bids))

    bids = [{"bid_id": b.bid_id, "item": b.item} for b in request.bids]
    result = await generate_hsn_codes(bids)

    if result.get("status") == "error":
        _log.warning("HSN generation returned error: %s", result.get("error_code"))

    return result
