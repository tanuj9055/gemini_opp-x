"""
Bid Submission Package Generator — Smart Gap-Fill Logic.

Core flow:
  1. Extract REQUIRED documents from bid_analysis
  2. Match vendor_documents against requirements → identify MISSING
  3. Generate ONLY the missing generatable documents via Gemini
  4. Assemble final package: existing + generated + flagged missing

ISOLATION: This service does NOT modify or depend on existing bid/vendor
analysis pipelines. It consumes their outputs (JSON) as read-only inputs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.logging_cfg import logger
from app.schemas import (
    BidAnalysisResponse,
    BidSubmissionPackageResponse,
    DocumentMetadata,
    DocumentSource,
    DocumentType,
    VendorEvaluationSummary,
)
from app.services.document_classifier import (
    classify_vendor_documents,
    extract_required_documents,
    get_missing_documents,
)
from app.services.gemini_client import generate, parse_json_response
from app.services.bid_package_prompts import (
    BID_PACKAGE_PROMPT,
    format_prompt,
)
from app.services.pdf_renderer import render_section_to_pdf, merge_pdfs

_log = logger.getChild("bid_package_generator")

# Generation temperature — LOW for deterministic output
_TEMPERATURE = 0.1


async def generate_bid_package(
    bid_analysis: BidAnalysisResponse,
    vendor_evaluation: VendorEvaluationSummary,
    vendor_documents: List[str],
) -> BidSubmissionPackageResponse:
    """Generate a bid submission package using smart gap-fill logic.

    Only generates documents that are MISSING and GENERATABLE.
    Standard vendor docs (PAN, GST, etc.) that are missing are
    flagged but NOT generated — they must be provided by the vendor.
    """

    # ── Step 1: Eligibility gate ─────────────────────
    # Eligibility check removed as per user request
    # Every vendor can generate a document now
    _log.info("Running gap analysis...")

    # ── Step 2: Extract required documents ───────────
    required = extract_required_documents(bid_analysis)
    _log.info("Required documents: %s", required)

    # ── Step 3: Gap analysis ─────────────────────────
    gap = get_missing_documents(required, vendor_documents)
    _log.info(
        "Gap analysis: %d required, %d present, %d missing "
        "(%d generatable, %d not-generatable)",
        len(gap.required),
        len(gap.present),
        len(gap.missing),
        len(gap.generatable),
        len(gap.not_generatable),
    )

    # ── Step 4: Classify existing vendor documents ───
    existing_doc_metadata = classify_vendor_documents(vendor_documents)

    # ── Step 5: Generate ONLY missing generatable docs
    generated_sections: Dict[str, Any] = {}
    generated_doc_metadata: List[DocumentMetadata] = []

    if gap.generatable:
        bid_json_str = json.dumps(
            bid_analysis.model_dump(mode="json", exclude={"raw_ocr_text"}),
            indent=2,
            ensure_ascii=False,
        )
        vendor_json_str = json.dumps(
            vendor_evaluation.model_dump(mode="json", exclude={"raw_ocr_text"}),
            indent=2,
            ensure_ascii=False,
        )

        try:
            _log.info("Generating unified bid package for missing sections: %s", gap.generatable)
            prompt = format_prompt(bid_json_str, vendor_json_str)

            raw_text, usage = await generate(
                prompt,
                file_handles=None,
                temperature=_TEMPERATURE,
                max_output_tokens=8192,
                response_mime_type="application/json",
            )

            parsed = parse_json_response(raw_text)
            
            # Allow fallback if model returns keys at top level instead of nesting
            bid_document = parsed.get("bid_document", parsed)
            _log.info(f"KEYS IN BID DOC: {list(bid_document.keys())}")
            
            # Extract generated sections that are needed based on gap.generatable
            for section_key in gap.generatable:
                section_content = bid_document.get(section_key, "Not Specified in Bid")
                _log.info(f"Extracted content for {section_key}: {type(section_content)}")
                generated_sections[section_key] = section_content

                generated_doc_metadata.append(
                    DocumentMetadata(
                        name=section_key,
                        type=DocumentType.GENERATED,
                        source=DocumentSource.GENERATED,
                        description=f"AI-generated {section_key.replace('_', ' ')}",
                    )
                )

            _log.info(
                "Unified sections generated (tokens: prompt=%s, completion=%s)",
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
            )

        except Exception as exc:
            import traceback
            traceback.print_exc()
            _log.error("Failed to generate unified bid package: %s", exc)
            for section_key in gap.generatable:
                generated_sections[section_key] = f"Generation failed: {exc}"
    else:
        _log.info("No generatable documents missing — skipping Gemini calls")

    # ── Step 6: Flag not-generatable missing docs ────
    missing_doc_metadata: List[DocumentMetadata] = []
    for category in gap.not_generatable:
        missing_doc_metadata.append(
            DocumentMetadata(
                name=category,
                type=DocumentType.STANDARD,
                source=DocumentSource.MISSING,
                description=f"MISSING: {category} — must be provided by vendor",
            )
        )

    # ── Step 7: Assemble final package ───────────────
    all_documents = existing_doc_metadata + generated_doc_metadata + missing_doc_metadata

    total_existing = len(existing_doc_metadata)
    total_generated = len(generated_doc_metadata)
    total_missing = len(missing_doc_metadata)

    _log.info(
        "Bid submission package assembled: %d documents "
        "(%d existing, %d generated, %d missing)",
        len(all_documents),
        total_existing,
        total_generated,
        total_missing,
    )

    return BidSubmissionPackageResponse(
        status="SUCCESS",
        message=(
            f"Package assembled: {total_existing} existing, "
            f"{total_generated} generated, {total_missing} still missing"
        ),
        documents=all_documents,
        generated_sections=generated_sections or None,
        gap_analysis=gap.to_dict(),
    )


async def generate_bid_package_pdf(
    bid_analysis: BidAnalysisResponse,
    vendor_evaluation: VendorEvaluationSummary,
    vendor_files: Dict[str, bytes],
) -> bytes:
    """Generate a fully assembled PDF of the bid submission package.
    
    1. Runs gap-fill generation.
    2. Renders generating sections to PDF.
    3. Merges generated PDFs with provided vendor PDFs.
    """
    bid_id = bid_analysis.bid_id or "UNKNOWN"
    
    # ── Step 1: Same gap-analysis / generation flow ──
    filenames = list(vendor_files.keys())
    package_result = await generate_bid_package(bid_analysis, vendor_evaluation, filenames)

    if package_result.status != "SUCCESS":
        raise ValueError(package_result.message)
        
    generated_sections = package_result.generated_sections or {}

    def _render(key: str) -> bytes:
        content = generated_sections.get(key, "Not Specified in Bid")
        return render_section_to_pdf(key, content, bid_id)

    # ── Step 2: Render generated sections to PDF ──
    pdf_cover_letter = _render("cover_letter")
    pdf_financial_comp = _render("financial_compliance")
    pdf_technical_comp = _render("technical_compliance")
    pdf_experience = _render("experience_statement")
    pdf_declaration = _render("declaration")

    # ── Step 3: Assemble PDF merge list in exact order ──
    # 1. Cover Letter
    pdf_list: List[tuple[str, bytes]] = [("AI_Cover_Letter.pdf", pdf_cover_letter)]
    
    # 2. Standard vendor documents
    for fname, fbytes in vendor_files.items():
        if fname.lower().endswith(".pdf"):
            pdf_list.append((fname, fbytes))
        else:
            _log.warning("Skipping non-PDF vendor file: %s", fname)
            
    # 3. Financial Compliance
    # 4. Technical Compliance
    # 5. Experience Statement
    # 6. Declaration / Undertaking
    pdf_list.extend([
        ("AI_Financial_Compliance.pdf", pdf_financial_comp),
        ("AI_Technical_Compliance.pdf", pdf_technical_comp),
        ("AI_Experience_Statement.pdf", pdf_experience),
        ("AI_Declaration.pdf", pdf_declaration)
    ])
    
    # ── Step 4: Merge ──
    _log.info("Merging %d PDF parts for bid %s", len(pdf_list), bid_id)
    merged_pdf_bytes = merge_pdfs(pdf_list)
    return merged_pdf_bytes
