"""
Test / Debug router — direct agent invocations without RabbitMQ.

All endpoints are prefixed with ``/test`` and exist purely for
development-time verification via Swagger / Postman.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

from app.logging_cfg import logger
from app.schemas import (
    AnalyzeBidTestRequest,
    ClassifyRulesRequest,
    ClassifyRulesResponse,
    EvaluateRulesRequest,
    EvaluateRulesResponse,
    ExtractRulesTestRequest,
    FilterRulesRequest,
    FilterRulesResponse,
    FullEligibilityRequest,
    FullEligibilityResponse,
    TenderAnalysisResult,
    TenderAnalysisEndpointResponse,
    TenderExtractionResult,
)

_log = logger.getChild("test_routes")

router = APIRouter(prefix="/test", tags=["Test / Debug"])


# ────────────────────────────────────────────────────────
# 1. Rule Extraction (existing agent)
# ────────────────────────────────────────────────────────

@router.post(
    "/extract-rules",
    response_model=TenderExtractionResult,
    summary="Test rule extraction agent",
    description="Calls the rule extractor passing OCR text and embedded links.",
)
async def test_extract_rules(body: ExtractRulesTestRequest):
    """Call existing rule extraction agent directly (no queue)."""
    job_id = uuid.uuid4().hex[:12]
    _log.info("📥 /test/extract-rules — request received  job_id=%s OCR length=%d", job_id, len(body.bidOcr))

    try:
        from app.agents.rule_extractor import extract_rules_from_text

        _log.info(
            "Agent start — extract_rules_from_text  job_id=%s  links=%d",
            job_id,
            len(body.embeddedLinkOcr),
        )
        links_data = [link.model_dump() for link in body.embeddedLinkOcr] if body.embeddedLinkOcr and hasattr(body.embeddedLinkOcr[0], "model_dump") else [dict(link) for link in body.embeddedLinkOcr] if body.embeddedLinkOcr else []
        result = await extract_rules_from_text(body.bidOcr, links_data)

        _log.info(
            "Agent output — extract_rules_from_text  job_id=%s  rules=%d",
            job_id,
            len(result.rules),
        )
        return result

    except Exception as exc:
        _log.error(
            "❌ /test/extract-rules FAILED — job_id=%s  error=%s",
            job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ────────────────────────────────────────────────────────
# 1b. Verifiable Eligibility Filter (Agent 5)
# ────────────────────────────────────────────────────────

@router.post(
    "/filter-rules",
    response_model=FilterRulesResponse,
    summary="Test verifiable eligibility filter",
    description="Classifies extracted rules as verifiable or non-verifiable.",
)
async def test_filter_rules(body: FilterRulesRequest):
    """Call verifiable filter agent directly (no queue)."""
    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📥 /test/filter-rules — request received  job_id=%s  criteria=%d",
        job_id,
        len(body.eligibility_criteria),
    )

    try:
        from app.agents.filter_agent import filter_rules

        _log.info("Agent start — filter_rules  job_id=%s", job_id)
        result = await filter_rules(body.get_criteria())

        _log.info(
            "Agent output — filter_rules  job_id=%s  verifiable=%d  non_verifiable=%d",
            job_id,
            len(result.verifiable_criteria),
            len(result.non_verifiable_criteria),
        )
        return result

    except Exception as exc:
        _log.error(
            "❌ /test/filter-rules FAILED — job_id=%s  error=%s",
            job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ────────────────────────────────────────────────────────
# 2. Bid Analysis (existing agent)
# ────────────────────────────────────────────────────────

@router.post(
    "/analyze-bid",
    response_model=TenderAnalysisEndpointResponse,
    summary="Test bid analysis agent",
    description="Calls the existing bid analyzer passing OCR text and embedded links.",
)
async def test_analyze_bid(body: ExtractRulesTestRequest):
    """Call existing bid analysis agent directly (no queue)."""
    job_id = uuid.uuid4().hex[:12]
    _log.info("📥 /test/analyze-bid — request received  job_id=%s  OCR length=%d", job_id, len(body.bidOcr))

    try:
        from app.agents.bid_analyzer import analyze_bid

        _log.info(
            "Agent start — analyze_bid  job_id=%s  links=%d",
            job_id,
            len(body.embeddedLinkOcr),
        )
        links_data = [link.model_dump() for link in body.embeddedLinkOcr] if body.embeddedLinkOcr and hasattr(body.embeddedLinkOcr[0], "model_dump") else [dict(link) for link in body.embeddedLinkOcr] if body.embeddedLinkOcr else []
        result = await analyze_bid(body.bidOcr, links_data)

        _log.info(
            "Agent output — analyze_bid  job_id=%s  highlights=%d  sections=%d",
            job_id,
            len(result.tender_analysis.highlights),
            len(result.tender_analysis.sections),
        )
        return TenderAnalysisEndpointResponse(tender_analysis=result.tender_analysis)

    except Exception as exc:
        _log.error(
            "❌ /test/analyze-bid FAILED — job_id=%s  error=%s",
            job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ────────────────────────────────────────────────────────
# 3. Rule Classification (new agent)
# ────────────────────────────────────────────────────────

@router.post(
    "/classify-rules",
    response_model=ClassifyRulesResponse,
    summary="Test rule classification agent",
    description="Classifies rules as checkable or non-checkable based on customer profile.",
)
async def test_classify_rules(body: ClassifyRulesRequest):
    """Call classification agent directly (no queue)."""
    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📥 /test/classify-rules — request received  job_id=%s  rules=%d",
        job_id,
        len(body.rules),
    )

    try:
        from app.agents.classification_agent import classify_rules

        _log.info("Agent start — classify_rules  job_id=%s", job_id)
        result = await classify_rules(body.rules, body.customer_profile)

        _log.info(
            "Agent output — classify_rules  job_id=%s  checkable=%d  non_checkable=%d",
            job_id,
            len(result.checkable_rules),
            len(result.non_checkable_rules),
        )
        return result

    except Exception as exc:
        _log.error(
            "❌ /test/classify-rules FAILED — job_id=%s  error=%s",
            job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ────────────────────────────────────────────────────────
# 4. Rule Evaluation (new agent)
# ────────────────────────────────────────────────────────

@router.post(
    "/evaluate-rules",
    response_model=EvaluateRulesResponse,
    summary="Test rule evaluation agent",
    description="Evaluates checkable rules against customer profile data.",
)
async def test_evaluate_rules(body: EvaluateRulesRequest):
    """Call evaluation agent directly (no queue)."""
    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📥 /test/evaluate-rules — request received  job_id=%s  rules=%d",
        job_id,
        len(body.checkable_rules),
    )

    try:
        from app.agents.evaluation_agent import evaluate_rules

        _log.info("Agent start — evaluate_rules  job_id=%s", job_id)
        result = await evaluate_rules(body.checkable_rules, body.customer_profile)

        _log.info(
            "Agent output — evaluate_rules  job_id=%s  passed=%d  failed=%d",
            job_id,
            len(result.passed),
            len(result.failed),
        )
        return result

    except Exception as exc:
        _log.error(
            "❌ /test/evaluate-rules FAILED — job_id=%s  error=%s",
            job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ────────────────────────────────────────────────────────
# 5. Full Eligibility (classification → evaluation)
# ────────────────────────────────────────────────────────

@router.post(
    "/full-eligibility",
    response_model=FullEligibilityResponse,
    summary="Test full eligibility flow",
    description="Chains classification then evaluation — no queues involved.",
)
async def test_full_eligibility(body: FullEligibilityRequest):
    """Run full eligibility: classify → evaluate."""
    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📥 /test/full-eligibility — request received  job_id=%s  rules=%d",
        job_id,
        len(body.rules),
    )

    try:
        from app.agents.classification_agent import classify_rules
        from app.agents.evaluation_agent import evaluate_rules

        # ── Step 1: Classification ──────────────────────────────
        _log.info("Full-eligibility step 1/2 — classify_rules  job_id=%s", job_id)
        classification = await classify_rules(body.rules, body.customer_profile)
        _log.info(
            "Classification done — checkable=%d  non_checkable=%d  job_id=%s",
            len(classification.checkable_rules),
            len(classification.non_checkable_rules),
            job_id,
        )

        # ── Step 2: Evaluation (only checkable rules) ───────────
        checkable_dicts = [r.model_dump() for r in classification.checkable_rules]
        _log.info(
            "Full-eligibility step 2/2 — evaluate_rules  job_id=%s  rules=%d",
            job_id,
            len(checkable_dicts),
        )
        evaluation = await evaluate_rules(checkable_dicts, body.customer_profile)
        _log.info(
            "Evaluation done — passed=%d  failed=%d  job_id=%s",
            len(evaluation.passed),
            len(evaluation.failed),
            job_id,
        )

        return FullEligibilityResponse(
            classification=classification,
            evaluation=evaluation,
        )

    except Exception as exc:
        _log.error(
            "❌ /test/full-eligibility FAILED — job_id=%s  error=%s",
            job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))
