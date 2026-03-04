"""
POST /analyze-bid

Accepts a single GeM Bid PDF, uploads it to the Gemini Files API,
and returns structured eligibility criteria extracted by gemini-1.5-pro.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.config import get_settings
from app.logging_cfg import logger
from app.schemas import BidAnalysisResponse, NormalizationMeta
from app.services.gemini_client import (
    QuotaExhaustedError,
    cleanup_files,
    generate,
    parse_json_response,
    upload_file,
)
from app.services.prompts import BID_ANALYSIS_PROMPT

_log = logger.getChild("bid_router")
router = APIRouter(tags=["Bid Analysis"])


@router.post(
    "/analyze-bid",
    response_model=BidAnalysisResponse,
    summary="Analyse a GeM Bid PDF and extract structured eligibility criteria",
    response_model_exclude_none=False,
)
async def analyze_bid(
    file: UploadFile = File(..., description="GeM Bid document (PDF)"),
) -> BidAnalysisResponse:
    """Upload a GeM bid document, run multimodal analysis via Gemini 1.5 Pro,
    and return eligibility criteria, EMD details, scope of work, risks, etc.
    """
    settings = get_settings()

    # ── Validate upload ──────────────────────────────
    if file.content_type not in ("application/pdf",):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Only PDF files are accepted. Got: {file.content_type}",
        )

    content = await file.read()
    if len(content) > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_file_size_mb} MB limit.",
        )

    # ── Persist temporarily ──────────────────────────
    tmp_dir = Path(tempfile.mkdtemp(prefix="gem_bid_"))
    tmp_path = tmp_dir / (file.filename or "bid.pdf")
    tmp_path.write_bytes(content)

    _log.info("Received bid PDF: %s (%.1f KB)", file.filename, len(content) / 1024)

    uploaded_handle = None
    try:
        # ── Upload to Gemini Files API ───────────────
        uploaded_handle = await upload_file(tmp_path, display_name=file.filename)

        # ── Build prompt ─────────────────────────────
        prompt = BID_ANALYSIS_PROMPT.format(filename=file.filename or "bid.pdf")

        # ── Generate ─────────────────────────────────
        raw_text, usage = await generate(prompt, file_handles=[uploaded_handle])

        # ── Parse ────────────────────────────────────
        data = parse_json_response(raw_text)

        if "_parse_error" in data:
            _log.error("Model returned unparseable JSON for bid %s", file.filename)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gemini returned invalid JSON. See server logs.",
            )

        # ── Normalize model output before validation ─
        data = _normalize_gemini_output(data)

        # ── Inject normalization meta ────────────────
        data["normalization_meta"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "model": usage.get("model"),
            "processing_time_seconds": usage.get("processing_time_seconds"),
        }

        _log.info(
            "Bid analysis complete – bid_id=%s  confidence_avg=%.2f  risks=%d",
            data.get("bid_id", "UNKNOWN"),
            _avg_confidence(data),
            len(data.get("risks", [])),
        )

        return BidAnalysisResponse(**data)

    except HTTPException:
        raise
    except TimeoutError as exc:
        _log.error("Bid analysis timed out: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except QuotaExhaustedError as exc:
        _log.error("Quota exhausted during bid analysis: %s", exc)
        raise HTTPException(
            status_code=429,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _log.exception("Unhandled error during bid analysis")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    finally:
        # ── Cleanup ──────────────────────────────────
        if uploaded_handle:
            await cleanup_files([uploaded_handle])
        tmp_path.unlink(missing_ok=True)
        tmp_dir.rmdir()


# ── Helpers ──────────────────────────────────────────────

_CLARITY_VALUES = {"CLEAR", "AMBIGUOUS", "NOT_FOUND"}
_COMPLIANCE_VALUES = {"UNKNOWN", "MET", "NOT_MET", "PARTIAL"}
_RISK_CATEGORIES = {
    "SYSTEMIC_GEM_RISK", "BUYER_ATC_RISK",
    "BID_SPECIFIC_COMPLIANCE_RISK", "VENDOR_DOCUMENT_RISK",
}
_SEVERITY_VALUES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def _normalize_gemini_output(data: dict) -> dict:
    """Fix common Gemini output quirks before Pydantic v2.0 validation.

    Handles:
    - v1 ``status`` → split into bid_requirement_clarity + vendor_compliance_status
    - required_value: wrap plain strings into StructuredRequirement
    - relaxations / similar_services_rules / risks: dict-of-dicts → list-of-dicts
    - risk category & severity normalisation
    """

    # ── Normalise eligibility_criteria ─────────────────
    for item in data.get("eligibility_criteria", []):
        if not isinstance(item, dict):
            continue

        # ---- v1 backward compat: migrate old ``status`` field ----
        if "status" in item and "bid_requirement_clarity" not in item:
            old = str(item.pop("status", "")).upper()
            if old in _CLARITY_VALUES:
                item["bid_requirement_clarity"] = old
            else:
                item["bid_requirement_clarity"] = "CLEAR"
            item.setdefault("vendor_compliance_status", "UNKNOWN")

        # ---- Ensure clarity & compliance have valid values ----
        brc = str(item.get("bid_requirement_clarity", "")).upper()
        if brc not in _CLARITY_VALUES:
            item["bid_requirement_clarity"] = "CLEAR"
        else:
            item["bid_requirement_clarity"] = brc

        vcs = str(item.get("vendor_compliance_status", "")).upper()
        if vcs not in _COMPLIANCE_VALUES:
            # During bid extraction, default to UNKNOWN
            item["vendor_compliance_status"] = "UNKNOWN"
        else:
            item["vendor_compliance_status"] = vcs

        # ---- required_value: wrap plain string/number into structured form ----
        rv = item.get("required_value")
        if rv is not None and not isinstance(rv, dict):
            item["required_value"] = {"raw_text": str(rv)}
            item.setdefault("required_value_raw", str(rv))

        # ---- Ensure criterion_id exists ----
        if not item.get("criterion_id"):
            name = item.get("criterion", "UNKNOWN")
            item["criterion_id"] = name.upper().replace(" ", "_").replace("(", "").replace(")", "")[:60]

        # ---- Clamp confidence ----
        conf = item.get("confidence")
        if conf is not None:
            try:
                item["confidence"] = max(0.0, min(1.0, float(conf)))
            except (ValueError, TypeError):
                item["confidence"] = 0.5

    # ── Normalise relaxations: dict → list ─────────────
    relaxations = data.get("relaxations")
    if isinstance(relaxations, dict):
        converted = []
        for key, val in relaxations.items():
            if isinstance(val, dict):
                entry = dict(val)
                entry.setdefault("criterion", key)
                entry.setdefault("criterion_id", key)
                converted.append(entry)
        data["relaxations"] = converted

    for item in data.get("relaxations", []):
        if not isinstance(item, dict):
            continue
        # Migrate v1 status → vendor_compliance_status
        if "status" in item and "vendor_compliance_status" not in item:
            old = str(item.pop("status")).upper()
            item["vendor_compliance_status"] = old if old in _COMPLIANCE_VALUES else "UNKNOWN"
        vcs = str(item.get("vendor_compliance_status", "")).upper()
        if vcs not in _COMPLIANCE_VALUES:
            item["vendor_compliance_status"] = "UNKNOWN"
        else:
            item["vendor_compliance_status"] = vcs
        item.setdefault("criterion_id", item.get("criterion", "UNKNOWN"))

    # ── Normalise similar_services_rules: dict → list ──
    ssr = data.get("similar_services_rules")
    if isinstance(ssr, dict):
        converted = []
        for key, val in ssr.items():
            if isinstance(val, dict):
                entry = dict(val)
                entry.setdefault("option_label", key)
                converted.append(entry)
        data["similar_services_rules"] = converted

    # ── Normalise risks: dict → list + taxonomy ────────
    risks = data.get("risks")
    if isinstance(risks, dict):
        converted = []
        for key, val in risks.items():
            if isinstance(val, dict):
                entry = dict(val)
                entry.setdefault("category", key)
                converted.append(entry)
        data["risks"] = converted

    for item in data.get("risks", []):
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "")).upper()
        if cat not in _RISK_CATEGORIES:
            item["category"] = "BID_SPECIFIC_COMPLIANCE_RISK"
        else:
            item["category"] = cat
        sev = str(item.get("severity", "")).upper()
        if sev not in _SEVERITY_VALUES:
            item["severity"] = "MEDIUM"
        else:
            item["severity"] = sev
        if not item.get("risk_id"):
            item["risk_id"] = f"RISK-{id(item) % 10000:04d}"

    # ── Normalise EMD ──────────────────────────────────
    emd = data.get("emd")
    if isinstance(emd, dict):
        emd.setdefault("currency", "INR")

    return data


def _avg_confidence(data: dict) -> float:
    """Compute average confidence across eligibility criteria."""
    criteria = data.get("eligibility_criteria", [])
    if not criteria:
        return 0.0
    confidences = [c.get("confidence", 0.0) for c in criteria if isinstance(c, dict)]
    return sum(confidences) / max(len(confidences), 1)
