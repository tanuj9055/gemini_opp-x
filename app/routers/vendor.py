"""
POST /evaluate-vendor

Accepts the extracted Bid JSON (from Stage 1) together with 6-7 vendor
PDF documents.  Gemini 1.5 Pro cross-references the documents to produce
an eligibility score, criterion-wise verdicts, risks, and a
recommendation (APPROVE / REJECT / REVIEW).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import List

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

from app.config import get_settings
from app.logging_cfg import logger
from app.schemas import VendorEvaluationResponse
from app.services.gemini_client import (
    QuotaExhaustedError,
    cleanup_files,
    generate,
    parse_json_response,
    upload_files,
)
from app.services.human_readable import inject_human_readable_vendor
from app.services.prompts import VENDOR_EVALUATION_PROMPT

_log = logger.getChild("vendor_router")
router = APIRouter(tags=["Vendor Evaluation"])


@router.post(
    "/evaluate-vendor",
    response_model=VendorEvaluationResponse,
    summary="Evaluate a vendor's eligibility against a GeM bid",
    response_model_exclude_none=False,
)
async def evaluate_vendor(
    bid_json: str = Form(
        ...,
        description=(
            "The JSON output from /analyze-bid (stringified). "
            "This provides the eligibility criteria the vendor will be measured against."
        ),
    ),
    files: List[UploadFile] = File(
        ...,
        description=(
            "Vendor supporting documents (6-7 PDFs): GST certificate, PAN card, "
            "balance sheets, work-order letters, address proof, Udyam / DPIIT certificate, etc."
        ),
    ),
) -> VendorEvaluationResponse:
    """Cross-reference multiple vendor documents against the bid criteria
    and return a scored eligibility assessment.
    """
    settings = get_settings()

    # ── Parse bid JSON ───────────────────────────────
    try:
        bid_data = json.loads(bid_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"bid_json is not valid JSON: {exc}",
        ) from exc

    # ── Validate vendor files ────────────────────────
    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one vendor PDF must be uploaded.",
        )

    for f in files:
        if f.content_type not in ("application/pdf",):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Only PDF files accepted. '{f.filename}' is {f.content_type}.",
            )

    _log.info(
        "Received vendor evaluation request – bid_id=%s  vendor_files=%d",
        bid_data.get("bid_id", "UNKNOWN"),
        len(files),
    )

    # ── Persist temporarily ──────────────────────────
    tmp_dir = Path(tempfile.mkdtemp(prefix="gem_vendor_"))
    tmp_paths: List[Path] = []
    uploaded_handles = []

    try:
        for f in files:
            content = await f.read()
            if len(content) > settings.max_file_size_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File '{f.filename}' exceeds {settings.max_file_size_mb} MB.",
                )
            p = tmp_dir / (f.filename or f"vendor_{len(tmp_paths)}.pdf")
            p.write_bytes(content)
            tmp_paths.append(p)

        # ── Upload all vendor PDFs concurrently ──────
        uploaded_handles = await upload_files(tmp_paths)

        # ── Build prompt ─────────────────────────────
        prompt = VENDOR_EVALUATION_PROMPT.format(
            bid_json=json.dumps(bid_data, indent=2, ensure_ascii=False),
            vendor_file_count=len(files),
        )

        # ── Generate ─────────────────────────────────
        raw_text, usage = await generate(prompt, file_handles=uploaded_handles)

        # ── Parse ────────────────────────────────────
        data = parse_json_response(raw_text)

        if "_parse_error" in data:
            _log.error("Model returned unparseable JSON for vendor evaluation")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gemini returned invalid JSON. See server logs.",
            )

        # ── Normalize model output before validation ─
        data = _normalize_vendor_output(data)

        # ── Inject human-readable requirement (vendor perspective) ──
        inject_human_readable_vendor(data)

        # ── Inject normalization meta ────────────────
        data["normalization_meta"] = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "model": usage.get("model"),
            "processing_time_seconds": usage.get("processing_time_seconds"),
        }

        score = data.get("eligibility_score", 0)
        recommendation = data.get("overall_recommendation", "UNKNOWN")
        _log.info(
            "Vendor evaluation complete – score=%s  recommendation=%s  risks=%d",
            score,
            recommendation,
            len(data.get("risks", [])),
        )

        # Log detailed criterion-wise breakdown (v2.0 field names)
        for key in ("financial_turnover", "experience", "similar_services", "location_verification"):
            finding = data.get(key)
            if isinstance(finding, dict):
                _log.info(
                    "  ├─ %s → %s (confidence=%.2f, risk=%s)",
                    key,
                    finding.get("vendor_compliance_status", "?"),
                    finding.get("confidence", 0),
                    finding.get("risk_level", "?"),
                )

        return VendorEvaluationResponse(**data)

    except HTTPException:
        raise
    except TimeoutError as exc:
        _log.error("Vendor evaluation timed out: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except QuotaExhaustedError as exc:
        _log.error("Quota exhausted during vendor evaluation: %s", exc)
        raise HTTPException(
            status_code=429,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _log.exception("Unhandled error during vendor evaluation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    finally:
        # ── Cleanup ──────────────────────────────────
        if uploaded_handles:
            await cleanup_files(uploaded_handles)
        for p in tmp_paths:
            p.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


# ── Helpers ──────────────────────────────────────────────

_COMPLIANCE_VALUES = {"UNKNOWN", "MET", "NOT_MET", "PARTIAL"}
_RISK_CATEGORIES = {
    "SYSTEMIC_GEM_RISK", "BUYER_ATC_RISK",
    "BID_SPECIFIC_COMPLIANCE_RISK", "VENDOR_DOCUMENT_RISK",
}
_SEVERITY_VALUES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def _normalize_vendor_output(data: dict) -> dict:
    """Fix common Gemini output quirks for v2.0 vendor evaluation.

    Handles:
    - v1 ``status`` → vendor_compliance_status on criterion-wise findings
    - required_value: wrap plain strings into StructuredRequirement dict
    - relaxations / risks: dict-of-dicts → list-of-dicts
    - risk category & severity normalisation
    - Ensure rejection_reasons is a list
    """

    # ── Normalise criterion-wise findings ──────────────
    for key in ("financial_turnover", "experience", "similar_services", "location_verification"):
        item = data.get(key)
        if isinstance(item, list):
            # Gemini sometimes returns [] instead of a dict – coerce to None
            data[key] = None
            continue
        if not isinstance(item, dict):
            continue
        _normalise_criterion(item, default_compliance="PARTIAL")

    # ── Normalise relaxations: dict → list ─────────────
    relaxations = data.get("relaxations")
    if isinstance(relaxations, dict):
        converted = []
        for k, v in relaxations.items():
            if isinstance(v, dict):
                entry = dict(v)
                entry.setdefault("criterion", k)
                entry.setdefault("criterion_id", k)
                converted.append(entry)
        data["relaxations"] = converted

    for item in data.get("relaxations", []):
        if not isinstance(item, dict):
            continue
        if "status" in item and "vendor_compliance_status" not in item:
            old = str(item.pop("status")).upper()
            item["vendor_compliance_status"] = old if old in _COMPLIANCE_VALUES else "PARTIAL"
        vcs = str(item.get("vendor_compliance_status", "")).upper()
        if vcs not in _COMPLIANCE_VALUES:
            item["vendor_compliance_status"] = "PARTIAL"
        else:
            item["vendor_compliance_status"] = vcs
        item.setdefault("criterion_id", item.get("criterion", "UNKNOWN"))

    # ── Normalise risks: dict → list + taxonomy ────────
    risks = data.get("risks")
    if isinstance(risks, dict):
        converted = []
        for k, v in risks.items():
            if isinstance(v, dict):
                entry = dict(v)
                entry.setdefault("category", k)
                converted.append(entry)
        data["risks"] = converted

    for item in data.get("risks", []):
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "")).upper()
        if cat not in _RISK_CATEGORIES:
            item["category"] = "VENDOR_DOCUMENT_RISK"
        else:
            item["category"] = cat
        sev = str(item.get("severity", "")).upper()
        if sev not in _SEVERITY_VALUES:
            item["severity"] = "MEDIUM"
        else:
            item["severity"] = sev
        if not item.get("risk_id"):
            item["risk_id"] = f"RISK-V{id(item) % 10000:04d}"

    # ── Ensure rejection_reasons is a list ─────────────
    rr = data.get("rejection_reasons")
    if rr is None:
        data["rejection_reasons"] = []
    elif isinstance(rr, str):
        data["rejection_reasons"] = [rr] if rr.strip() else []

    # ── Ensure acceptance_reasons is a list ────────────
    ar = data.get("acceptance_reasons")
    if ar is None:
        data["acceptance_reasons"] = []
    elif isinstance(ar, str):
        data["acceptance_reasons"] = [ar] if ar.strip() else []

    # ── Validate overall_recommendation ────────────────
    rec = str(data.get("overall_recommendation", "")).upper()
    if rec not in {"APPROVE", "REJECT"}:
        data["overall_recommendation"] = "REJECT"
    else:
        data["overall_recommendation"] = rec

    return data


def _normalise_criterion(item: dict, *, default_compliance: str = "PARTIAL") -> None:
    """Normalise a single criterion-wise finding dict (v2.0).

    Migrates v1 ``status`` → split fields, wraps required_value, etc.
    """
    # ---- Migrate v1 status → split fields ----
    if "status" in item and "vendor_compliance_status" not in item:
        old = str(item.pop("status")).upper()
        item["vendor_compliance_status"] = old if old in _COMPLIANCE_VALUES else default_compliance

    vcs = str(item.get("vendor_compliance_status", "")).upper()
    if vcs not in _COMPLIANCE_VALUES:
        item["vendor_compliance_status"] = default_compliance
    else:
        item["vendor_compliance_status"] = vcs

    # bid_requirement_clarity — carry from bid JSON or default
    brc = str(item.get("bid_requirement_clarity", "")).upper()
    if brc not in {"CLEAR", "AMBIGUOUS", "NOT_FOUND"}:
        item["bid_requirement_clarity"] = "CLEAR"
    else:
        item["bid_requirement_clarity"] = brc

    # ---- required_value: wrap plain string ----
    rv = item.get("required_value")
    if rv is not None and not isinstance(rv, dict):
        item["required_value"] = {"raw_text": str(rv)}
        item.setdefault("required_value_raw", str(rv))

    # ---- Generate criterion_id if missing ----
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
