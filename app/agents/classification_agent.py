"""
Rule Classification Agent — Agent 3.

Classifies extracted rules into checkable vs non-checkable
based on whether the customer_profile contains the required fields.

DOES NOT evaluate rules. DOES NOT infer missing data.
ONLY checks field availability in customer_profile.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List

from app.logging_cfg import logger
from app.schemas import ClassifyRulesResponse
from app.services.gemini_client import generate, parse_json_response
from app.services.prompts import RULE_CLASSIFICATION_PROMPT

_log = logger.getChild("classification_agent")


async def classify_rules(
    rules: List[Dict[str, Any]],
    customer_profile: Dict[str, Any],
) -> ClassifyRulesResponse:
    """Classify rules as checkable or non-checkable.

    A rule is **checkable** if the customer_profile contains all the
    fields required to evaluate it.  A rule is **non-checkable** if one
    or more required fields are missing.

    Parameters
    ----------
    rules : list[dict]
        Extracted eligibility rules (from Agent 1).
    customer_profile : dict
        Structured customer data sent by NetJS backend.

    Returns
    -------
    ClassifyRulesResponse
        Checkable and non-checkable rule lists with field mappings.
    """
    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📋 Classification STARTED — job_id=%s  rule_count=%d",
        job_id,
        len(rules),
    )
    t0 = time.perf_counter()

    try:
        # ── Step 1: Build prompt with rules + profile ────────────
        prompt = RULE_CLASSIFICATION_PROMPT.format(
            rules_json=json.dumps(rules, indent=2, ensure_ascii=False),
            customer_profile_json=json.dumps(customer_profile, indent=2, ensure_ascii=False),
        )
        _log.debug(
            "[classify_rules] Prompt built — prompt_chars=%d  job_id=%s",
            len(prompt),
            job_id,
        )

        # ── Step 2: Call Gemini ──────────────────────────────────
        _log.info(
            "Sending classification prompt to Gemini — job_id=%s",
            job_id,
        )
        raw_text, usage = await generate(
            prompt,
            temperature=0.1,  # Low temperature for deterministic classification
        )
        generation_elapsed = time.perf_counter() - t0
        _log.info(
            "[classify_rules] Gemini generation complete — "
            "elapsed=%.2fs  prompt_tokens=%s  completion_tokens=%s  job_id=%s",
            generation_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            job_id,
        )

        # ── Step 3: Parse JSON response ─────────────────────────
        _log.debug("[classify_rules] Parsing JSON response (raw_len=%d)", len(raw_text))
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

        # ── Step 4: Validate through Pydantic schema ────────────
        data.setdefault("checkable_rules", [])
        data.setdefault("non_checkable_rules", [])

        result = ClassifyRulesResponse(**data)

        # ── Step 5: Log completion ──────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Classification COMPLETED — job_id=%s  "
            "checkable=%d  non_checkable=%d  elapsed=%.2fs",
            job_id,
            len(result.checkable_rules),
            len(result.non_checkable_rules),
            total_elapsed,
        )
        return result

    except RuntimeError:
        raise

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ Classification FAILED — job_id=%s  elapsed=%.2fs  error=%s",
            job_id,
            elapsed,
            exc,
            exc_info=True,
        )
        raise
