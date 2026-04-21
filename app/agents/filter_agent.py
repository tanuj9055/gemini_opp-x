"""
Verifiable Eligibility Filter Agent — Agent 5.

Classifies extracted eligibility criteria into verifiable vs non-verifiable.
Verifiable = can be proven with documents/data before bid submission.
Non-verifiable = process rules, post-award, compliance boilerplate.

DOES NOT evaluate rules. DOES NOT modify rule text.
ONLY classifies verifiability.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List

from app.logging_cfg import logger
from app.schemas import FilterRulesResponse
from app.services.gemini_client import generate, parse_json_response
from app.services.prompts import VERIFIABLE_FILTER_PROMPT

_log = logger.getChild("filter_agent")


async def filter_rules(
    eligibility_criteria: List[Dict[str, Any]],
) -> FilterRulesResponse:
    """Classify eligibility criteria as verifiable or non-verifiable."""

    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📋 Verifiable filter STARTED — job_id=%s  criteria_count=%d",
        job_id,
        len(eligibility_criteria),
    )
    t0 = time.perf_counter()

    try:
        # ── Step 1: Build prompt ──────────────────────────────
        criteria_json = json.dumps(
            {"eligibility_criteria": eligibility_criteria},
            indent=2,
            ensure_ascii=False,
        )
        prompt = VERIFIABLE_FILTER_PROMPT.format(
            eligibility_criteria_json=criteria_json,
        )

        _log.debug(
            "[filter_rules] Prompt built — prompt_chars=%d  job_id=%s",
            len(prompt),
            job_id,
        )

        # ── Step 2: Call Gemini ───────────────────────────────
        _log.info(
            "Sending verifiable filter prompt to Gemini — job_id=%s",
            job_id,
        )

        raw_text, usage = await generate(
            prompt,
            temperature=0.1,
        )

        generation_elapsed = time.perf_counter() - t0

        _log.info(
            "[filter_rules] Gemini generation complete — "
            "elapsed=%.2fs  prompt_tokens=%s  completion_tokens=%s  job_id=%s",
            generation_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            job_id,
        )

        # ── Step 3: Parse JSON ───────────────────────────────
        _log.debug(
            "[filter_rules] Parsing JSON response (raw_len=%d)",
            len(raw_text),
        )

        data = parse_json_response(raw_text)

        if not isinstance(data, dict):
            _log.error("❌ Unexpected JSON type from Gemini: %s", type(data))
            raise RuntimeError(f"Unexpected JSON type: {type(data)}")

        if "_parse_error" in data:
            _log.error(
                "❌ Gemini returned unparseable JSON — error=%s  raw_preview=%s",
                data.get("_parse_error"),
                data.get("_raw_text", "")[:200],
            )
            raise RuntimeError(f"JSON parsing failed: {data.get('_parse_error')}")

        # ── Step 4: Ensure required keys ─────────────────────
        data.setdefault("verifiable_criteria", [])
        data.setdefault("non_verifiable_criteria", [])

        # ── Step 5: Validate with Pydantic ───────────────────
        result = FilterRulesResponse(**data)

        # ── Step 6: Log completion ───────────────────────────
        total_elapsed = time.perf_counter() - t0

        _log.info(
            "✅ Verifiable filter COMPLETED — job_id=%s  "
            "verifiable=%d  non_verifiable=%d  elapsed=%.2fs",
            job_id,
            len(result.verifiable_criteria),
            len(result.non_verifiable_criteria),
            total_elapsed,
        )

        return result

    except RuntimeError:
        raise

    except Exception as exc:
        elapsed = time.perf_counter() - t0

        _log.error(
            "❌ Verifiable filter FAILED — job_id=%s  elapsed=%.2fs  error=%s",
            job_id,
            elapsed,
            exc,
            exc_info=True,
        )

        raise
