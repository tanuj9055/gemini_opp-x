"""
Rule Evaluation Agent — Agent 4.

Evaluates ONLY checkable rules against customer_profile data.

MUST use customer data exclusively.
MUST provide reasoning and evidence for each verdict.
MUST NOT hallucinate missing data.
MUST NOT evaluate non-checkable rules.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List

from app.logging_cfg import logger
from app.schemas import EvaluateRulesResponse
from app.services.gemini_client import generate, parse_json_response
from app.services.prompts import RULE_EVALUATION_PROMPT

_log = logger.getChild("evaluation_agent")


async def evaluate_rules(
    checkable_rules: List[Dict[str, Any]],
    customer_profile: Dict[str, Any],
) -> EvaluateRulesResponse:
    """Evaluate checkable rules against customer profile data.

    Parameters
    ----------
    checkable_rules : list[dict]
        Rules that have all required data in customer_profile
        (output from classification agent).
    customer_profile : dict
        Structured customer data sent by NestJS backend.

    Returns
    -------
    EvaluateRulesResponse
        Passed and failed rules with reasoning and evidence.
    """
    job_id = uuid.uuid4().hex[:12]
    _log.info(
        "📋 Evaluation STARTED — job_id=%s  rule_count=%d",
        job_id,
        len(checkable_rules),
    )
    t0 = time.perf_counter()

    if not checkable_rules:
        _log.info(
            "⚠️ No checkable rules provided — returning empty result  job_id=%s",
            job_id,
        )
        return EvaluateRulesResponse(passed=[], failed=[])

    try:
        # ── Step 1: Build prompt with rules + profile ────────────
        prompt = RULE_EVALUATION_PROMPT.format(
            checkable_rules_json=json.dumps(checkable_rules, indent=2, ensure_ascii=False),
            customer_profile_json=json.dumps(customer_profile, indent=2, ensure_ascii=False),
        )
        _log.debug(
            "[evaluate_rules] Prompt built — prompt_chars=%d  job_id=%s",
            len(prompt),
            job_id,
        )

        # ── Step 2: Call Gemini ──────────────────────────────────
        _log.info(
            "Sending evaluation prompt to Gemini — job_id=%s",
            job_id,
        )
        raw_text, usage = await generate(
            prompt,
            temperature=0.1,  # Deterministic evaluation
        )
        generation_elapsed = time.perf_counter() - t0
        _log.info(
            "[evaluate_rules] Gemini generation complete — "
            "elapsed=%.2fs  prompt_tokens=%s  completion_tokens=%s  job_id=%s",
            generation_elapsed,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            job_id,
        )

        # ── Step 3: Parse JSON response ─────────────────────────
        _log.debug("[evaluate_rules] Parsing JSON response (raw_len=%d)", len(raw_text))
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
        data.setdefault("passed", [])
        data.setdefault("failed", [])

        result = EvaluateRulesResponse(**data)

        # ── Step 5: Log completion ──────────────────────────────
        total_elapsed = time.perf_counter() - t0
        _log.info(
            "✅ Evaluation COMPLETED — job_id=%s  "
            "passed=%d  failed=%d  elapsed=%.2fs",
            job_id,
            len(result.passed),
            len(result.failed),
            total_elapsed,
        )
        return result

    except RuntimeError:
        raise

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ Evaluation FAILED — job_id=%s  elapsed=%.2fs  error=%s",
            job_id,
            elapsed,
            exc,
            exc_info=True,
        )
        raise
