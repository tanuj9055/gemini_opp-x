"""
POST /process-bid-evaluation

Orchestrates the complete procurement audit pipeline:
  1. Download bid PDF from S3
  2. Run bid analysis (Stage 1) via the existing /analyze-bid logic
  3. For each vendor: download docs from S3, run vendor evaluation (Stage 2)
  4. Inject human_readable_requirement into eligibility criteria
  5. Return aggregated results

This endpoint is designed to be invoked by Pub/Sub triggers or backend services.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status

from app.config import get_settings
from app.logging_cfg import logger
from app.schemas import (
    BidAnalysisResponse,
    BidEvaluationRequest,
    BidEvaluationResponse,
    EligibilityCriterion,
    NormalizationMeta,
    VendorEvaluationResponse,
    VendorEvaluationSummary,
)
from app.services.gemini_client import (
    QuotaExhaustedError,
    cleanup_files,
    generate,
    parse_json_response,
    upload_file,
    upload_files,
)
from app.services.human_readable import (
    generate_human_readable_requirement,
    generate_vendor_human_readable,
    inject_human_readable_bid,
    inject_human_readable_vendor,
)
from app.services.prompts import BID_ANALYSIS_PROMPT, VENDOR_EVALUATION_PROMPT
from app.services.s3_client import download_file, download_files

_log = logger.getChild("orchestrator")
router = APIRouter(tags=["Orchestration"])


# Re-export for backward compatibility (worker imports these)
generate_human_readable = generate_human_readable_requirement
inject_human_readable = inject_human_readable_bid


# ────────────────────────────────────────────────────────
# Bid analysis (reuses existing logic from bid.py)
# ────────────────────────────────────────────────────────

# Import normalization helper from bid router
from app.routers.bid import _normalize_gemini_output


async def _run_bid_analysis(bid_pdf_path: Path, filename: str) -> BidAnalysisResponse:
    """Run Stage 1 bid analysis on a local PDF. Reuses existing Gemini logic."""
    uploaded_handle = None
    try:
        uploaded_handle = await upload_file(bid_pdf_path, display_name=filename)

        prompt = BID_ANALYSIS_PROMPT.format(filename=filename)
        raw_text, usage = await generate(prompt, file_handles=[uploaded_handle])

        data = parse_json_response(raw_text)
        if "_parse_error" in data:
            raise RuntimeError("Gemini returned unparseable JSON for bid analysis.")

        data = _normalize_gemini_output(data)

        # Inject human-readable requirements
        inject_human_readable(data.get("eligibility_criteria", []))

        data["normalization_meta"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "model": usage.get("model"),
            "processing_time_seconds": usage.get("processing_time_seconds"),
        }

        return BidAnalysisResponse(**data)

    finally:
        if uploaded_handle:
            await cleanup_files([uploaded_handle])


# ────────────────────────────────────────────────────────
# Vendor evaluation (reuses existing logic from vendor.py)
# ────────────────────────────────────────────────────────

from app.routers.vendor import _normalize_vendor_output


async def _run_vendor_evaluation(
    bid_analysis: BidAnalysisResponse,
    vendor_doc_paths: List[Path],
) -> VendorEvaluationResponse:
    """Run Stage 2 vendor evaluation on local PDFs. Reuses existing Gemini logic."""
    uploaded_handles = []
    try:
        uploaded_handles = await upload_files(vendor_doc_paths)

        bid_data = bid_analysis.model_dump(mode="json")
        prompt = VENDOR_EVALUATION_PROMPT.format(
            bid_json=json.dumps(bid_data, indent=2, ensure_ascii=False),
            vendor_file_count=len(vendor_doc_paths),
        )

        raw_text, usage = await generate(prompt, file_handles=uploaded_handles)

        data = parse_json_response(raw_text)
        if "_parse_error" in data:
            raise RuntimeError("Gemini returned unparseable JSON for vendor evaluation.")

        data = _normalize_vendor_output(data)

        data["normalization_meta"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "model": usage.get("model"),
            "processing_time_seconds": usage.get("processing_time_seconds"),
        }

        return VendorEvaluationResponse(**data)

    finally:
        if uploaded_handles:
            await cleanup_files(uploaded_handles)


# ────────────────────────────────────────────────────────
# Helper: build criterion verdicts from vendor evaluation
# ────────────────────────────────────────────────────────

def _extract_criterion_verdicts(vendor_eval: VendorEvaluationResponse) -> List[Dict[str, Any]]:
    """Extract the criterion-wise verdicts from VendorEvaluationResponse
    and inject human_readable_requirement (vendor perspective)."""
    verdicts = []
    for field in ("financial_turnover", "experience", "similar_services", "location_verification"):
        criterion = getattr(vendor_eval, field, None)
        if criterion is not None:
            d = criterion.model_dump(mode="json")
            d["human_readable_requirement"] = generate_vendor_human_readable(d)
            verdicts.append(d)
    return verdicts


# ────────────────────────────────────────────────────────
# Helper: generate summary text
# ────────────────────────────────────────────────────────

def _generate_summary(
    bid_id: str,
    vendor_summaries: List[VendorEvaluationSummary],
) -> str:
    """Generate a human-readable summary of the evaluation."""
    if not vendor_summaries:
        return f"Bid {bid_id}: No vendors were evaluated."

    parts = []
    for vs in vendor_summaries:
        if vs.error:
            parts.append(f"Vendor {vs.vendor_id}: evaluation failed ({vs.error}).")
        elif vs.recommendation == "APPROVE":
            reasons = "; ".join(vs.acceptance_reasons[:3]) if vs.acceptance_reasons else "criteria met"
            parts.append(
                f"Vendor {vs.vendor_id} scored {vs.eligibility_score:.0f}/100 "
                f"and is recommended for approval ({reasons})."
            )
        elif vs.recommendation == "REJECT":
            reasons = "; ".join(vs.rejection_reasons[:3]) if vs.rejection_reasons else "criteria not met"
            parts.append(
                f"Vendor {vs.vendor_id} scored {vs.eligibility_score:.0f}/100 "
                f"and is recommended for rejection ({reasons})."
            )
        else:
            parts.append(
                f"Vendor {vs.vendor_id} scored {vs.eligibility_score:.0f}/100 "
                f"and requires manual review."
            )

    return " ".join(parts)


# ────────────────────────────────────────────────────────
# Main endpoint
# ────────────────────────────────────────────────────────

@router.post(
    "/process-bid-evaluation",
    response_model=BidEvaluationResponse,
    summary="Orchestrate the complete bid analysis + vendor evaluation pipeline",
    response_model_exclude_none=False,
)
async def process_bid_evaluation(
    request: BidEvaluationRequest,
) -> BidEvaluationResponse:
    """End-to-end procurement audit pipeline.

    1. Download bid PDF from S3
    2. Extract eligibility criteria (Stage 1)
    3. For each vendor: download documents from S3, evaluate (Stage 2)
    4. Return aggregated results with human-readable requirement explanations

    Designed to be called by Pub/Sub triggers or backend services.
    """
    _log.info(
        "Starting bid evaluation pipeline – bid_id=%s  vendors=%d",
        request.bid_id,
        len(request.vendors),
    )

    errors: List[str] = []
    bid_analysis: Optional[BidAnalysisResponse] = None
    vendor_summaries: List[VendorEvaluationSummary] = []

    # Create a top-level temp directory for all downloads
    tmp_root = Path(tempfile.mkdtemp(prefix="gem_pipeline_"))

    try:
        # ── Step 1: Download bid PDF from S3 ─────────
        bid_tmp_dir = tmp_root / "bid"
        bid_tmp_dir.mkdir()

        try:
            bid_pdf_path = await download_file(request.bid_document_url, bid_tmp_dir)
        except Exception as exc:
            _log.error("Failed to download bid document: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to download bid document from {request.bid_document_url}: {exc}",
            )

        _log.info("Bid PDF downloaded: %s", bid_pdf_path)

        # ── Step 2: Run bid analysis (Stage 1) ───────
        try:
            bid_analysis = await _run_bid_analysis(
                bid_pdf_path,
                filename=bid_pdf_path.name,
            )
            _log.info(
                "Bid analysis complete – bid_id=%s  criteria=%d",
                bid_analysis.bid_id,
                len(bid_analysis.eligibility_criteria),
            )
        except QuotaExhaustedError as exc:
            _log.error("Quota exhausted during bid analysis: %s", exc)
            raise HTTPException(status_code=429, detail=str(exc))
        except TimeoutError as exc:
            _log.error("Bid analysis timed out: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=str(exc),
            )
        except Exception as exc:
            _log.exception("Bid analysis failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Bid analysis failed: {exc}",
            )

        # ── Step 3 & 4: Process each vendor ──────────
        for vendor_input in request.vendors:
            vendor_tmp_dir = tmp_root / f"vendor_{vendor_input.vendor_id}"
            vendor_tmp_dir.mkdir()

            _log.info(
                "Processing vendor %s – documents=%d",
                vendor_input.vendor_id,
                len(vendor_input.documents),
            )

            try:
                # Download vendor documents
                vendor_doc_paths = await download_files(
                    vendor_input.documents, vendor_tmp_dir,
                )

                # Run vendor evaluation (Stage 2)
                vendor_result = await _run_vendor_evaluation(
                    bid_analysis,
                    vendor_doc_paths,
                )

                # Build criterion verdicts with human-readable explanations
                criterion_verdicts_raw = _extract_criterion_verdicts(vendor_result)
                criterion_verdicts = [
                    EligibilityCriterion(**c) for c in criterion_verdicts_raw
                ]

                vendor_summaries.append(
                    VendorEvaluationSummary(
                        vendor_id=vendor_input.vendor_id,
                        eligibility_score=vendor_result.eligibility_score,
                        recommendation=vendor_result.overall_recommendation,
                        criterion_verdicts=criterion_verdicts,
                        vendor_profile=vendor_result.vendor_profile,
                        rejection_reasons=vendor_result.rejection_reasons,
                        risks=vendor_result.risks,
                    )
                )

                _log.info(
                    "Vendor %s evaluated – score=%.0f  recommendation=%s",
                    vendor_input.vendor_id,
                    vendor_result.eligibility_score,
                    vendor_result.overall_recommendation,
                )

            except QuotaExhaustedError as exc:
                _log.error("Quota exhausted for vendor %s: %s", vendor_input.vendor_id, exc)
                vendor_summaries.append(
                    VendorEvaluationSummary(
                        vendor_id=vendor_input.vendor_id,
                        error=f"Quota exhausted: {exc}",
                    )
                )
                errors.append(f"Vendor {vendor_input.vendor_id}: Quota exhausted – {exc}")
                # Stop processing further vendors if quota is exhausted
                break

            except Exception as exc:
                _log.exception("Vendor %s evaluation failed", vendor_input.vendor_id)
                vendor_summaries.append(
                    VendorEvaluationSummary(
                        vendor_id=vendor_input.vendor_id,
                        error=str(exc),
                    )
                )
                errors.append(f"Vendor {vendor_input.vendor_id}: {exc}")
                # Continue to next vendor

        # ── Step 5: Aggregate & return ───────────────
        summary = _generate_summary(request.bid_id, vendor_summaries)

        return BidEvaluationResponse(
            bid_id=request.bid_id,
            bid_analysis=bid_analysis,
            vendor_evaluations=vendor_summaries,
            summary=summary,
            errors=errors,
        )

    finally:
        # ── Cleanup all temp files ───────────────────
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
            _log.debug("Cleaned up temp directory: %s", tmp_root)
        except Exception as exc:
            _log.warning("Failed to cleanup temp dir %s: %s", tmp_root, exc)
