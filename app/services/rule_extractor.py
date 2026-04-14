"""
Tender Rule Extraction Service — Agent 1.

Extracts structured eligibility rules from tender PDFs using Gemini.
NO evaluation logic. NO vendor assessment. ONLY rule extraction.

Uses the existing Gemini client infrastructure (upload_file, generate,
parse_json_response) and validates output through Pydantic schemas.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from app.logging_cfg import logger
from app.schemas import TenderExtractionResult
from app.services.gemini_client import (
    cleanup_files,
    generate,
    parse_json_response,
    upload_file,
)
from app.services.prompts import RULE_EXTRACTION_PROMPT

_log = logger.getChild("rule_extractor")


async def extract_rules(tender_pdf_path: Path) -> TenderExtractionResult:
    """Extract structured eligibility rules from a tender PDF.

    Parameters
    ----------
    tender_pdf_path : Path
        Local path to the downloaded tender PDF.

    Returns
    -------
    TenderExtractionResult
        Validated extraction result with structured rules.

    Raises
    ------
    RuntimeError
        If Gemini returns unparseable JSON.
    """
    filename = tender_pdf_path.name
    _log.info(
        "📋 Rule extraction STARTED — file=%s  size=%.1f KB",
        filename,
        tender_pdf_path.stat().st_size / 1024,
    )
    t0 = time.perf_counter()

    uploaded_handle = None
    try:
        # ── Step 1: Upload PDF to Gemini ─────────────────────────
        _log.debug("[extract_rules] Uploading PDF to Gemini: %s", filename)
        uploaded_handle = await upload_file(tender_pdf_path, display_name=filename)
        upload_elapsed = time.perf_counter() - t0
        _log.debug(
            "[extract_rules] Upload complete — elapsed=%.2fs", upload_elapsed
        )

        # ── Step 2: Call Gemini with extraction prompt ───────────
        _log.debug("[extract_rules] Sending extraction prompt to Gemini")
        prompt = RULE_EXTRACTION_PROMPT  # No format vars needed for this prompt
        raw_text, usage = await generate(
            prompt,
            file_handles=[uploaded_handle],
            temperature=0.1,  # Low temperature for deterministic extraction
        )
        generation_elapsed = time.perf_counter() - t0
        _log.info(
            "[extract_rules] Gemini generation complete — "
            "elapsed=%.2fs  prompt_tokens=%s  completion_tokens=%s",
            generation_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        # ── Step 3: Parse JSON response ──────────────────────────
        _log.debug("[extract_rules] Parsing JSON response (raw_len=%d)", len(raw_text))
        try:
            data = parse_json_response(raw_text)
        except Exception as e:
            _log.error("❌ JSON parse failed: %s | raw=%s", e, raw_text[:500])
            raise RuntimeError(f"JSON parsing failed: {e}")

        # ── Step 4: Normalize structure (handle raw lists) ───────
        if isinstance(data, list):
            _log.info("[extract_rules] Received raw list of %d rules (no wrapper)", len(data))
            data = {
                "tender_id": "UNKNOWN",
                "rules": data,
                "risk": [],
                "metadata": {},
                "raw_ocr_text": None
            }
        
        if not isinstance(data, dict):
            _log.error("❌ Unexpected JSON type from Gemini: %s", type(data))
            raise RuntimeError(f"Unexpected JSON type from Gemini: {type(data)}")

        if "_parse_error" in data:
            _log.error(
                "❌ [extract_rules] Gemini returned unparseable JSON — "
                "error=%s  raw_preview=%s",
                data.get("_parse_error"),
                data.get("_raw_text", "")[:200],
            )
            raise RuntimeError(
                f"Gemini returned unparseable JSON for rule extraction: "
                f"{data.get('_parse_error')}"
            )

        # ── Step 5: Safe fallback handling ────────────────────
        data["tender_id"] = data.get("tender_id") or "UNKNOWN"
        data["rules"] = data.get("rules") or []
        data["risk"] = data.get("risk") or []

        rules_captured = len(data.get("rules", []))
        risk_captured = len(data.get("risk", []))
        _log.debug(
            "[extract_rules] Validating response — rules_count=%d  risk_count=%d  keys=%s",
            rules_captured,
            risk_captured,
            list(data.keys()),
        )

        result = TenderExtractionResult(**data)

        # ── Step 6: Log completion ───────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Rule extraction COMPLETED — "
            "tender_id=%s  rules=%d  risk=%d  elapsed=%.2fs  "
            "prompt_tokens=%s  completion_tokens=%s",
            result.tender_id,
            len(result.rules),
            len(result.risk),
            total_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        # Debug logging: extracted counts
        _log.debug("Extracted Rules: %d", len(result.rules))
        _log.debug("Extracted Risks: %d", len(result.risk))

        # Log individual rules at DEBUG level for traceability
        for rule in result.rules:
            _log.debug(
                "  📌 Rule: id=%s  type=%s  operator=%s  value=%s  unit=%s  "
                "confidence=%.2f  desc=%s",
                rule.id,
                rule.type.value,
                rule.operator.value,
                rule.value,
                rule.unit,
                rule.confidence,
                rule.description[:80] if rule.description else "",
            )

        # Log individual risks at DEBUG level
        for risk_item in result.risk:
            _log.debug(
                "  ⚠️ Risk: type=%s  desc=%s",
                risk_item.type,
                risk_item.description[:80] if risk_item.description else "",
            )

        return result

    except RuntimeError:
        raise  # Re-raise RuntimeError as-is (JSON parse failure)

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ Rule extraction FAILED — file=%s  elapsed=%.2fs  error=%s",
            filename,
            elapsed,
            exc,
            exc_info=True,
        )
        raise

    finally:
        if uploaded_handle:
            _log.debug("[extract_rules] Cleaning up uploaded file handle")
            await cleanup_files([uploaded_handle])
