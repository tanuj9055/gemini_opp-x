"""
Tender Analysis Service — Agent 2.

Analyzes tender PDFs for scope of work, key requirements, and risks using Gemini.
Runs independently of rule extraction.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.logging_cfg import logger
from app.schemas import TenderAnalysisResult
from app.services.gemini_client import (
    cleanup_files,
    generate,
    parse_json_response,
    upload_file,
)
from app.services.prompts import BID_ANALYSIS_INSIGHTS_PROMPT

_log = logger.getChild("bid_analyzer")


async def analyze_bid(ocr_text: str, embedded_links_ocr: list) -> TenderAnalysisResult:
    """Analyze a tender using OCR text and embedded links for insights (Scope, Requirements, Risks).

    Parameters
    ----------
    ocr_text : str
        The raw text extracted via OCR.
    embedded_links_ocr : list
        List of dictionaries with OCR'd text from embedded links.

    Returns
    -------
    TenderAnalysisResult
        Validated analysis result with insights and risks.

    Raises
    ------
    RuntimeError
        If Gemini returns unparseable JSON or fails.
    """
    _log.info(
        "📋 Bid analysis STARTED — text_len=%d  links=%d",
        len(ocr_text),
        len(embedded_links_ocr),
    )
    t0 = time.perf_counter()

    try:
        # ── Step 1: Prepare Prompt ───────────────────────────────
        _log.debug("[analyze_bid] Preparing analysis prompt for Gemini")
        prompt = BID_ANALYSIS_INSIGHTS_PROMPT
        prompt += f"\n\n--- TENDER OCR TEXT ---\n{ocr_text}\n"
        
        if embedded_links_ocr:
            links_text = "\n\n".join(
                f"--- CONTENT FROM LINK: {link.get('sourceUrl', 'Unknown') if isinstance(link, dict) else getattr(link, 'sourceUrl', 'Unknown')} ---\n{link.get('ocrText', '') if isinstance(link, dict) else getattr(link, 'ocrText', '')}" 
                for link in embedded_links_ocr
            )
            prompt += f"\n\nAdditional Content from Embedded Links:\n{links_text}\n"

        # ── Step 2: Call Gemini with analysis prompt ─────────────
        _log.debug("[analyze_bid] Sending analysis prompt to Gemini")
        raw_text, usage = await generate(
            prompt,
            file_handles=None,
            temperature=0.2,
        )
        generation_elapsed = time.perf_counter() - t0
        _log.info(
            "[analyze_bid] Gemini generation complete — "
            "elapsed=%.2fs  prompt_tokens=%s  completion_tokens=%s",
            generation_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        # ── Step 3: Parse JSON response ──────────────────────────
        _log.debug("[analyze_bid] Parsing JSON response (raw_len=%d)", len(raw_text))
        try:
            data = parse_json_response(raw_text)
        except Exception as e:
            _log.error("❌ JSON parse failed: %s | raw=%s", e, raw_text[:500])
            raise RuntimeError(f"JSON parsing failed: {e}")

        # ── Step 4: Validate and Fix Structure ───────────────────
        if not isinstance(data, dict):
            _log.error("❌ Unexpected JSON type from Gemini: %s", type(data))
            raise RuntimeError(f"Unexpected JSON type from Gemini: {type(data)}")

        if "_parse_error" in data:
            _log.error(
                "❌ [analyze_bid] Gemini returned unparseable JSON — "
                "error=%s  raw_preview=%s",
                data.get("_parse_error"),
                data.get("_raw_text", "")[:200],
            )
            raise RuntimeError(
                f"Gemini returned unparseable JSON for bid analysis: "
                f"{data.get('_parse_error')}"
            )

        # Log raw LLM output
        _log.info("📝 Raw LLM Output: %s", data)

        # ── Step 5: Validate, clean, and map through Pydantic schema ─
        tender_id = data.get("tender_id", "UNKNOWN")
        metadata = data.get("metadata", {})

        # Extract the tender_analysis block if it exists
        analysis_block = data.get("tender_analysis", {})
        if not analysis_block:
            # Fallback if the agent returned the direct/old structure
            analysis_block = data

        def ensure_list(val):
            return val if isinstance(val, list) else []

        summary = analysis_block.get("summary", data.get("summary", ""))
        highlights = ensure_list(analysis_block.get("highlights", data.get("highlights", [])))
        raw_sections = ensure_list(analysis_block.get("sections", data.get("sections", [])))

        # ── Clean sections: strip priority, enforce importance, remove empties ──
        clean_sections = []
        for sec in raw_sections:
            if not isinstance(sec, dict):
                continue
            # Strip priority field if present
            sec.pop("priority", None)

            raw_points = sec.get("points", [])
            clean_points = []
            for pt in raw_points:
                if isinstance(pt, str):
                    # Legacy format: plain string → convert to object
                    pt = {"text": pt, "importance": "medium"}
                if isinstance(pt, dict) and pt.get("text"):
                    pt.pop("priority", None)
                    if "importance" not in pt:
                        pt["importance"] = "medium"
                    clean_points.append(pt)

            if clean_points:
                sec["points"] = clean_points
                clean_sections.append(sec)

        tender_analysis = {
            "summary": summary,
            "highlights": highlights,
            "sections": clean_sections,
        }

        _log.info("📊 Parsed Structured Output: %s", tender_analysis)

        result = TenderAnalysisResult(
            tender_id=tender_id,
            tender_analysis=tender_analysis,
            metadata=metadata,
        )

        # ── Step 6: Log completion ───────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Bid analysis COMPLETED — "
            "tender_id=%s  highlights=%d  sections=%d  elapsed=%.2fs  "
            "prompt_tokens=%s  completion_tokens=%s",
            result.tender_id,
            len(result.tender_analysis.highlights),
            len(result.tender_analysis.sections),
            total_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )

        return result

    except RuntimeError:
        raise

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ Bid analysis FAILED — elapsed=%.2fs  error=%s",
            elapsed,
            exc,
            exc_info=True,
        )
        raise