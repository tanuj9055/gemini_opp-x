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


async def extract_rules_from_text(ocr_text: str, embedded_links_ocr: list) -> TenderExtractionResult:
    """Extract structured eligibility rules from raw OCR text and embedded links OCR."""
    _log.info("📋 Rule extraction STARTED (from OCR text) — text_len=%d  links=%d", len(ocr_text), len(embedded_links_ocr))
    t0 = time.perf_counter()

    try:
        # Construct the prompt
        prompt = RULE_EXTRACTION_PROMPT.replace("{tender_text}", ocr_text)
        if embedded_links_ocr:
            links_text = "\n\n".join(
                f"--- CONTENT FROM LINK: {link.get('sourceUrl', 'Unknown') if isinstance(link, dict) else getattr(link, 'sourceUrl', 'Unknown')} ---\n{link.get('ocrText', '') if isinstance(link, dict) else getattr(link, 'ocrText', '')}" 
                for link in embedded_links_ocr
            )
            prompt += f"\n\nAdditional Content from Embedded Links:\n{links_text}\n"

        _log.debug("[extract_rules_from_text] Sending extraction prompt to Gemini")
        raw_text, usage = await generate(
            prompt,
            file_handles=None,
            temperature=0.1,
        )
        generation_elapsed = time.perf_counter() - t0
        _log.info(
            "[extract_rules_from_text] Gemini generation complete — "
            "elapsed=%.2fs  prompt_tokens=%s  completion_tokens=%s",
            generation_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        _log.debug("[extract_rules_from_text] Parsing JSON response")
        try:
            data = parse_json_response(raw_text)
        except Exception as e:
            _log.error("❌ JSON parse failed: %s | raw=%s", e, raw_text[:500])
            raise RuntimeError(f"JSON parsing failed: {e}")

        if isinstance(data, list):
            data = {
                "tender_id": "UNKNOWN",
                "rules": data,
                "metadata": {},
                "raw_ocr_text": None
            }

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected JSON type from Gemini: {type(data)}")

        if "_parse_error" in data:
            raise RuntimeError(f"Gemini returned unparseable JSON: {data.get('_parse_error')}")

        # ── Remap eligibility_criteria → rules (current prompt uses this key) ──
        if "eligibility_criteria" in data and "rules" not in data:
            _log.debug("[extract_rules_from_text] Remapping 'eligibility_criteria' → 'rules'")
            data["rules"] = data.pop("eligibility_criteria")

        data["tender_id"] = data.get("tender_id") or "UNKNOWN"
        data["rules"] = data.get("rules") or []
        data["raw_ocr_text"] = ocr_text
        data["metadata"] = data.get("metadata") or {}

        result = TenderExtractionResult(**data)

        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Rule extraction COMPLETED (from text) — rules=%d  elapsed=%.2fs",
            len(result.rules),
            total_elapsed,
        )
        return result

    except RuntimeError:
        raise
    except Exception as exc:
        _log.error("❌ Rule extraction FAILED (from text) — error=%s", exc, exc_info=True)
        raise


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
        # Remap eligibility_criteria → rules (current prompt uses this key)
        if "eligibility_criteria" in data and "rules" not in data:
            _log.debug("[extract_rules] Remapping 'eligibility_criteria' → 'rules'")
            data["rules"] = data.pop("eligibility_criteria")

        data["tender_id"] = data.get("tender_id") or "UNKNOWN"
        data["rules"] = data.get("rules") or []

        _log.debug(
            "[extract_rules] Validating response — rules_count=%d  keys=%s",
            len(data.get("rules", [])),
            list(data.keys()),
        )

        result = TenderExtractionResult(**data)

        # ── Step 6: Log completion ───────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Rule extraction COMPLETED — "
            "tender_id=%s  rules=%d  elapsed=%.2fs  "
            "prompt_tokens=%s  completion_tokens=%s",
            result.tender_id,
            len(result.rules),
            total_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        _log.debug("Extracted Rules: %d", len(result.rules))

        # Log individual rules at DEBUG level for traceability
        for rule in result.rules:
            _log.debug(
                "  📌 Rule: id=%s  text=%s",
                rule.id,
                (rule.text[:80] if rule.text else ""),
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
