"""
Job processor – runs the full bid evaluation pipeline for a single job.

Reuses the existing Gemini-based analysis functions from the orchestrator,
S3 download helpers, and normalization logic.  This module is pure business
logic with NO RabbitMQ or HTTP dependencies so it can be tested in isolation.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from app.logging_cfg import logger
from app.services.s3_client import download_file, download_files

# Reuse the existing orchestrator helpers (Gemini pipeline, normalization, etc.)
from app.routers.orchestrator import (
    _extract_criterion_verdicts,
    _generate_summary,
    _run_bid_analysis,
    _run_vendor_evaluation,
    inject_human_readable,
)

_log = logger.getChild("job_processor")

# Weights for deterministic eligibility score calculation
_CRITERION_WEIGHTS = {
    "financial_turnover": 25,
    "experience": 25,
    "similar_services": 25,
    "location_verification": 15,
}
_RELAXATION_WEIGHT = 10


def _compute_eligibility_score(vendor_eval) -> int:
    """Compute a deterministic eligibility score from criterion verdicts.

    Weights: Financial 25% | Experience 25% | Similar Services 25%
             | Location 15% | Relaxations 10%
    MET = full weight, PARTIAL = half weight, NOT_MET = 0.
    """
    score = 0.0
    for field, weight in _CRITERION_WEIGHTS.items():
        criterion = getattr(vendor_eval, field, None)
        if criterion is None:
            continue
        status = getattr(criterion, "vendor_compliance_status", None)
        if status is not None:
            status = str(status.value if hasattr(status, "value") else status).upper()
        if status == "MET":
            score += weight
        elif status == "PARTIAL":
            score += weight * 0.5

    # Relaxations: 10% total, split evenly across applicable relaxations
    relaxations = vendor_eval.relaxations or []
    applicable = [r for r in relaxations if r.is_applicable]
    if applicable:
        per_relaxation = _RELAXATION_WEIGHT / len(applicable)
        for r in applicable:
            status = r.vendor_compliance_status
            if status is not None:
                status = str(status.value if hasattr(status, "value") else status).upper()
            if status == "MET":
                score += per_relaxation
            elif status == "PARTIAL":
                score += per_relaxation * 0.5

    return round(score)


# ────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────

async def process_evaluation_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the full bid-evaluation pipeline for *job* and return a result dict.

    Parameters
    ----------
    job : dict
        Decoded JSON message from the ``bid_evaluation_jobs`` queue.
        Expected keys: ``job_id``, ``bid_id``, ``bid_document_url``, ``vendors``.

    Returns
    -------
    dict
        A result message ready to be published to ``bid_evaluation_results``.
    """
    job_id = job.get("job_id", "UNKNOWN")
    bid_id = job.get("bid_id", "UNKNOWN")
    bid_document_url = job.get("bid_document_url", "")
    vendors = job.get("vendors", [])

    _log.info(
        "⚙️  Job %s started – bbid_id=%s  vendors=%d",
        job_id, bid_id, len(vendors),
    )
    t0 = time.perf_counter()

    # Sanitise job_id for use in filesystem paths (bid numbers contain '/')
    safe_job_id = job_id.replace("/", "_").replace("\\", "_")

    # Top-level temp directory for the entire job
    tmp_root = Path(tempfile.mkdtemp(prefix=f"gem_job_{safe_job_id}_"))
    errors: List[str] = []
    vendor_results: List[Dict[str, Any]] = []

    try:
        # ── Step 1: Download bid PDF from S3 ─────────────────────────
        bid_tmp_dir = tmp_root / "bid"
        bid_tmp_dir.mkdir()

        _log.info("[%s] Downloading bid document from %s", job_id, bid_document_url)
        try:
            bid_pdf_path = await download_file(bid_document_url, bid_tmp_dir)
        except Exception as exc:
            _log.error("[%s] Bid download failed: %s", job_id, exc)
            return _build_error_result(job_id, bid_id, f"Bid download failed: {exc}")

        # ── Step 2: Run bid analysis (Stage 1) ──────────────────────
        _log.info("[%s] Running bid analysis …", job_id)
        try:
            bid_analysis = await _run_bid_analysis(bid_pdf_path, filename=bid_pdf_path.name)
        except Exception as exc:
            _log.error("[%s] Bid analysis failed: %s", job_id, exc)
            return _build_error_result(job_id, bid_id, f"Bid analysis failed: {exc}")

        _log.info(
            "[%s] Bid analysis complete – criteria=%d",
            job_id, len(bid_analysis.eligibility_criteria),
        )

        # ── Step 3: Evaluate each vendor ─────────────────────────────
        for vendor_input in vendors:
            vendor_id = vendor_input.get("vendor_id", "UNKNOWN")
            doc_urls: List[str] = vendor_input.get("documents", [])

            vendor_tmp_dir = tmp_root / f"vendor_{vendor_id}"
            vendor_tmp_dir.mkdir(exist_ok=True)

            _log.info("[%s] Processing vendor %s – documents=%d", job_id, vendor_id, len(doc_urls))

            try:
                # Download vendor documents from S3
                vendor_doc_paths = await download_files(doc_urls, vendor_tmp_dir)

                # Run vendor evaluation (Stage 2)
                vendor_eval = await _run_vendor_evaluation(bid_analysis, vendor_doc_paths)

                # Extract criterion verdicts with human-readable labels
                criterion_verdicts = _extract_criterion_verdicts(vendor_eval)

                # Compute deterministic eligibility score from verdicts
                score = _compute_eligibility_score(vendor_eval)
                recommendation = "APPROVED" if score >= 60 else "REJECT"

                # Build acceptance_reasons from criteria that are MET
                acceptance_reasons = [

                    f"{v['criterion']}: {v.get('detail', '')}"                    
                    for v in criterion_verdicts
                    if v.get("vendor_compliance_status") == "MET"
                ]

                # Serialize relaxations and risks
                relaxations = [r.model_dump(mode="json") for r in (vendor_eval.relaxations or [])]
                risks = [r.model_dump(mode="json") for r in (vendor_eval.risks or [])]
                vendor_profile = vendor_eval.vendor_profile.model_dump(mode="json") if vendor_eval.vendor_profile else None

                vendor_results.append({
                    "vendor_id": vendor_id,
                    "eligibility_score": score,
                    "recommendation": recommendation,
                    "criterion_verdicts": criterion_verdicts,
                    "rejection_reasons": vendor_eval.rejection_reasons or [],
                    "acceptance_reasons": acceptance_reasons,
                    "vendor_profile": vendor_profile,
                    "relaxations": relaxations,
                    "risks": risks,
                })

                _log.info(
                    "[%s] Vendor %s – score=%.0f  recommendation=%s",
                    job_id, vendor_id,
                    score,
                    recommendation,
                )

            except Exception as exc:
                _log.exception("[%s] Vendor %s evaluation failed", job_id, vendor_id)
                vendor_results.append({
                    "vendor_id": vendor_id,
                    "error": str(exc),
                })
                errors.append(f"Vendor {vendor_id}: {exc}")
                # Continue processing remaining vendors

        # ── Step 4: Build result message ─────────────────────────────
        elapsed = time.perf_counter() - t0

        # Serialize the FULL bid analysis response (endpoint 1 data)
        bid_analysis_dict = bid_analysis.model_dump(mode="json")

        # Inject human-readable text into every bid analysis criterion
        inject_human_readable(bid_analysis_dict.get("eligibility_criteria", []))

        result = {
            "job_id": job_id,
            "bid_id": bid_id,
            "status": "completed",
            "bid_analysis": bid_analysis_dict,
            "vendor_results": vendor_results,
            "errors": errors,
            "processing_time_seconds": round(elapsed, 2),
        }

        _log.info(
            "✅ Job %s completed – vendors=%d  errors=%d  elapsed=%.1fs",
            job_id, len(vendor_results), len(errors), elapsed,
        )
        return result

    except Exception as exc:
        _log.exception("[%s] Unhandled error in job processor", job_id)
        return _build_error_result(job_id, bid_id, f"Unhandled error: {exc}")

    finally:
        # ── Cleanup all temporary files ──────────────────────────────
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
            _log.debug("[%s] Cleaned up temp directory: %s", job_id, tmp_root)
        except Exception as exc:
            _log.warning("[%s] Temp cleanup failed: %s", job_id, exc)


# ────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────

def _build_error_result(job_id: str, bid_id: str, error: str) -> Dict[str, Any]:
    """Build a standardised error result message."""
    _log.error("❌ Job %s failed – %s", job_id, error)
    return {
        "job_id": job_id,
        "bid_id": bid_id,
        "status": "failed",
        "error": error,
        "vendor_results": [],
        "errors": [error],
    }
