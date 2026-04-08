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


async def analyze_bid(tender_pdf_path: Path) -> TenderAnalysisResult:
    """Analyze a tender PDF for insights (Scope, Requirements, Risks).

    Parameters
    ----------
    tender_pdf_path : Path
        Local path to the downloaded tender PDF.

    Returns
    -------
    TenderAnalysisResult
        Validated analysis result with insights and risks.

    Raises
    ------
    RuntimeError
        If Gemini returns unparseable JSON or fails.
    """
    filename = tender_pdf_path.name
    _log.info(
        "📋 Bid analysis STARTED — file=%s  size=%.1f KB",
        filename,
        tender_pdf_path.stat().st_size / 1024,
    )
    t0 = time.perf_counter()

    uploaded_handle = None
    try:
        # ── Step 1: Upload PDF to Gemini ─────────────────────────
        _log.debug("[analyze_bid] Uploading PDF to Gemini: %s", filename)
        uploaded_handle = await upload_file(tender_pdf_path, display_name=filename)
        upload_elapsed = time.perf_counter() - t0
        _log.debug(
            "[analyze_bid] Upload complete — elapsed=%.2fs", upload_elapsed
        )

        # ── Step 2: Call Gemini with analysis prompt ─────────────
        _log.debug("[analyze_bid] Sending analysis prompt to Gemini")
        prompt = BID_ANALYSIS_INSIGHTS_PROMPT
        raw_text, usage = await generate(
            prompt,
            file_handles=[uploaded_handle],
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

        # ── Step 5: Validate through Pydantic schema ─────────────
        data.setdefault("tender_id", "UNKNOWN")
        data.setdefault("scope_of_work", "")
        data.setdefault("key_requirements", [])
        data.setdefault("risks", [])
        data.setdefault("metadata", {})

        result = TenderAnalysisResult(**data)

        # ── Step 6: Log completion ───────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Bid analysis COMPLETED — "
            "tender_id=%s  reqs=%d  risks=%d  elapsed=%.2fs  "
            "prompt_tokens=%s  completion_tokens=%s",
            result.tender_id,
            len(result.key_requirements),
            len(result.risks),
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
            "❌ Bid analysis FAILED — file=%s  elapsed=%.2fs  error=%s",
            filename,
            elapsed,
            exc,
            exc_info=True,
        )
        raise

    finally:
        if uploaded_handle:
            _log.debug("[analyze_bid] Cleaning up uploaded file handle")
            await cleanup_files([uploaded_handle])