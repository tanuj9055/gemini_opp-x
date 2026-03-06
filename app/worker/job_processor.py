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
from app.services.human_readable import inject_human_readable_vendor

# Reuse the existing orchestrator helpers (Gemini pipeline, normalization, etc.)
from app.routers.orchestrator import (
    _extract_criterion_verdicts,
    _generate_summary,
    _run_bid_analysis,
    _run_vendor_evaluation,
    inject_human_readable,
)

_log = logger.getChild("job_processor")


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

                # Serialize the FULL vendor evaluation response
                vendor_eval_dict = vendor_eval.model_dump(mode="json")

                # Inject human-readable into the full vendor data as well
                inject_human_readable_vendor(vendor_eval_dict)

                vendor_results.append({
                    "vendor_id": vendor_id,
                    "eligibility_score": vendor_eval.eligibility_score,
                    "recommendation": vendor_eval.overall_recommendation,
                    "criterion_verdicts": criterion_verdicts,
                    "rejection_reasons": vendor_eval.rejection_reasons or [],
                    "full_evaluation": vendor_eval_dict,
                })

                _log.info(
                    "[%s] Vendor %s – score=%.0f  recommendation=%s",
                    job_id, vendor_id,
                    vendor_eval.eligibility_score,
                    vendor_eval.overall_recommendation,
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
