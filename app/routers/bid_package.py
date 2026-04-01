"""
POST /generate-bid-package

Generates a complete Bid Submission Package by combining:
  - Existing vendor documents (classified by filename)
  - AI-generated sections (cover letter, compliance, etc.)

This router is COMPLETELY ISOLATED from the existing bid/vendor endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
import json
import io

from app.logging_cfg import logger
from app.schemas import (
    BidSubmissionPackageRequest,
    BidSubmissionPackageResponse,
    BidAnalysisResponse,
    VendorEvaluationResponse,
)
from app.services.bid_package_generator import generate_bid_package, generate_bid_package_pdf
from app.services.gemini_client import QuotaExhaustedError

_log = logger.getChild("bid_package_router")

router = APIRouter(tags=["Bid Submission Package"])


@router.post(
    "/generate-bid-package",
    response_model=BidSubmissionPackageResponse,
    summary="Generate a complete bid submission package",
    description=(
        "Accepts the bid analysis (Stage 1) and vendor evaluation (Stage 2) "
        "outputs along with vendor document filenames, and generates a complete "
        "bid submission package including AI-generated cover letter, compliance "
        "statements, and declarations.\n\n"
        "**Prerequisite**: The vendor must have an APPROVE recommendation. "
        "If the vendor is REJECTED, an immediate rejection response is returned "
        "without any Gemini API calls."
    ),
    response_model_exclude_none=False,
)
async def create_bid_package(
    request: BidSubmissionPackageRequest,
) -> BidSubmissionPackageResponse:
    """Generate a bid submission package from bid analysis + vendor evaluation."""
    bid_id = request.bid_analysis.bid_id or "UNKNOWN"
    recommendation = (request.vendor_evaluation.overall_recommendation or "").upper()

    _log.info(
        "POST /generate-bid-package – bid_id=%s  recommendation=%s  docs=%d",
        bid_id,
        recommendation,
        len(request.vendor_documents),
    )

    try:
        result = await generate_bid_package(
            bid_analysis=request.bid_analysis,
            vendor_evaluation=request.vendor_evaluation,
            vendor_documents=request.vendor_documents,
        )

        _log.info(
            "Bid package result – bid_id=%s  status=%s  documents=%d",
            bid_id,
            result.status,
            len(result.documents),
        )

        return result

    except TimeoutError as exc:
        _log.error("Bid package generation timed out: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except QuotaExhaustedError as exc:
        _log.error("Quota exhausted during bid package generation: %s", exc)
        raise HTTPException(
            status_code=429,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _log.exception("Unhandled error during bid package generation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post(
    "/generate-bid-package-pdf",
    summary="Generate a merged PDF for the bid submission package",
    description=(
        "Accepts stringified bid_analysis and vendor_evaluation JSON, "
        "plus actual vendor PDF uploads, and returns a single merged PDF."
    )
)
async def create_bid_package_pdf(
    bid_json: str = Form(...),
    vendor_json: str = Form(...),
    files: list[UploadFile] = File(default=[])
) -> StreamingResponse:
    try:
        bid_data = json.loads(bid_json)
        vendor_data = json.loads(vendor_json)
        
        bid_analysis = BidAnalysisResponse(**bid_data)
        vendor_evaluation = VendorEvaluationResponse(**vendor_data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON data: {e}")

    vendor_files_dict = {}
    for upload_file in files:
        if upload_file.filename:
            content = await upload_file.read()
            vendor_files_dict[upload_file.filename] = content

    try:
        pdf_bytes = await generate_bid_package_pdf(
            bid_analysis=bid_analysis,
            vendor_evaluation=vendor_evaluation,
            vendor_files=vendor_files_dict,
        )

        # Basic check if it returned a JSON error (starts with '{')
        if pdf_bytes.startswith(b'{'):
            raise HTTPException(status_code=400, detail=json.loads(pdf_bytes.decode("utf-8")))
            
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=\"Bid_Submission_Package_{bid_analysis.bid_id or 'UNKNOWN'}.pdf\""
            }
        )

    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unhandled error during PDF bid package generation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

