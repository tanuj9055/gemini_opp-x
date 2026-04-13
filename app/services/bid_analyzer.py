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

        # Log raw LLM output
        _log.info("📝 Raw LLM Output: %s", data)

        # ── Step 5: Validate and Map through Pydantic schema ─────
        tender_id = data.get("tender_id", "UNKNOWN")
        metadata = data.get("metadata", {})

        # Extract the tender_analysis block if it exists
        analysis_block = data.get("tender_analysis", {})
        if not analysis_block:
            # Fallback if the agent returned the direct/old structure
            analysis_block = data

        def ensure_list(val):
            return val if isinstance(val, list) else []

        # Technical Requirements mapping
        tech_reqs = analysis_block.get("technical_requirements")
        if tech_reqs is None:
            old_keys = ensure_list(data.get("key_requirements", []))
            tech_reqs = [{"id": f"TR-{i}", "requirement": req, "type": "Legacy"} if isinstance(req, str) else req for i, req in enumerate(old_keys)]
        else:
            tech_reqs = ensure_list(tech_reqs)

        # Scope of Work mapping
        sow_field = analysis_block.get("scope_of_work")
        if sow_field is None or isinstance(sow_field, str):
            sow_str = sow_field if isinstance(sow_field, str) else data.get("scope_of_work", "")
            sow_list = [{"summary": sow_str}] if sow_str else []
        else:
            sow_list = ensure_list(sow_field)

        # Risks mapping
        risks_field = ensure_list(analysis_block.get("risks", data.get("risks", [])))
        risks_list = []
        for r in risks_field:
            if isinstance(r, str):
                risks_list.append({"risk": r, "severity": "medium"})
            elif isinstance(r, dict):
                risk_text = r.get("risk", r.get("description", r.get("title", "Unknown Risk")))
                severity = str(r.get("severity", "medium")).lower()
                if severity not in ["low", "medium", "high"]: severity = "medium"
                risks_list.append({"risk": risk_text, "severity": severity})

        tender_analysis = {
            "technical_requirements": tech_reqs,
            "commercial_terms": ensure_list(analysis_block.get("commercial_terms", [])),
            "important_dates": ensure_list(analysis_block.get("important_dates", [])),
            "evaluation_criteria": ensure_list(analysis_block.get("evaluation_criteria", [])),
            "scope_of_work": sow_list,
            "risks": risks_list
        }

        _log.info("📝 Parsed Structured Output: %s", tender_analysis)

        result = TenderAnalysisResult(
            tender_id=tender_id,
            tender_analysis=tender_analysis,
            metadata=metadata
        )

        # ── Step 6: Log completion ───────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Bid analysis COMPLETED — "
            "tender_id=%s  tech_reqs=%d  risks=%d  elapsed=%.2fs  "
            "prompt_tokens=%s  completion_tokens=%s",
            result.tender_id,
            len(result.tender_analysis.technical_requirements),
            len(result.tender_analysis.risks),
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