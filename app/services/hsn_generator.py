"""
HSN Code Generation Service.

Uses Vertex AI (Gemini) to classify government tender items into HSN
(Harmonized System of Nomenclature) codes.  Receives bid items,
sends a structured prompt to Gemini, and returns validated HSN results.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List

from dotenv import load_dotenv

# MUST be called before importing vertexai
load_dotenv() 

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

# Added Tenacity for rate limit handling
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from google.api_core.exceptions import ResourceExhausted

from app.logging_cfg import logger
from app.services.gemini_client import parse_json_response

_log = logger.getChild("hsn_generator")

# ────────────────────────────────────────────────────────
# Initialize Vertex AI
# It will automatically use GOOGLE_APPLICATION_CREDENTIALS from the .env
# ────────────────────────────────────────────────────────
vertexai.init(project="qistonpe-project-22810", location="us-central1")
hsn_model = GenerativeModel("gemini-2.5-flash-lite")

# ────────────────────────────────────────────────────────
# System prompt for HSN classification
# ────────────────────────────────────────────────────────

HSN_SYSTEM_PROMPT = """\
You are an expert HSN (Harmonized System of Nomenclature) code classifier \
specialized in Indian government procurement and tender classification.

Your task is to analyze government tender items and assign accurate HSN codes based on:
1. Item descriptions and specifications
2. Ministry/department context
3. Indian HSN classification standards
4. Common procurement patterns in government tenders

CRITICAL RULES FOR HSN CODES:
- HSN codes MUST be pure digit strings with NO dots, NO decimals, NO punctuation.
- HSN codes MUST be 4, 6, or 8 digits long. Nothing else.
- NEVER return "N/A", "NA", empty strings, or null for hsn. If you are unsure, \
return your best guess with confidence "low".
- WRONG: "8482.50.00", "9987.00.00", "N/A", ""
- CORRECT: "84825000", "99870000", "84714100"
- If the item description is a service (not goods), use the correct SAC/HSN \
service code as a pure digit string (e.g. "998719" not "9987.19.00").

Always return valid JSON in the exact format requested.
Provide confidence levels (high/medium/low) based on description clarity.

The response MUST include the bid_id field from the input for each bid.

Return ONLY the JSON object — no markdown, no commentary.
"""


def _build_user_prompt(bids: List[Dict[str, str]]) -> str:
    """Build the user prompt that lists all bid items to classify."""
    lines = [
        "Classify the following government tender items and return their HSN codes.",
        "",
        "Return a JSON object with this structure:",
        '{',
        '  "results": [',
        '    {',
        '      "bid_id": "<bid_id from input>",',
        '      "hsn": "<6 or 8 digit HSN code>",',
        '      "confidence": "high|medium|low",',
        '      "reasoning": "Brief explanation of the classification"',
        '    }',
        '  ]',
        '}',
        "",
        "Items to classify:",
        "",
    ]
    for i, bid in enumerate(bids, 1):
        lines.append(f"{i}. bid_id: {bid['bid_id']}")
        lines.append(f"   Item: {bid['item']}")
        lines.append("")

    return "\n".join(lines)


def _sanitise_hsn(raw_hsn: Any, bid_id: Any = "") -> str:
    """Clean an HSN code to a pure digit string of 4–8 characters.

    Handles common Gemini quirks:
      - Decimal notation:  ``8482.50.00`` → ``84825000``
      - Dotted pairs:      ``9987.00.00`` → ``99870000``
      - N/A / null / empty → ``"000000"`` (flagged with warning)
      - Leading/trailing whitespace
    """
    if raw_hsn is None:
        raw_hsn = ""
    hsn = str(raw_hsn).strip()

    # Reject obvious non-values
    if not hsn or hsn.upper() in ("N/A", "NA", "NULL", "NONE", "-"):
        _log.warning("HSN for bid %s was '%s' — defaulting to '000000'", bid_id, raw_hsn)
        return "000000"

    # Remove dots / periods (e.g. "8482.50.00" → "84825000")
    hsn = hsn.replace(".", "")

    # Strip any remaining non-digit characters
    hsn = re.sub(r"\D", "", hsn)

    if not hsn:
        _log.warning("HSN for bid %s was '%s' (no digits) — defaulting to '000000'", bid_id, raw_hsn)
        return "000000"

    # Pad to at least 4 digits
    if len(hsn) < 4:
        hsn = hsn.ljust(4, "0")

    # Truncate to max 8 digits
    if len(hsn) > 8:
        hsn = hsn[:8]

    return hsn


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences (```json … ```) from model output."""
    if not text:
        return text
    text = re.sub(r"^```[\w]*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    return text.strip()


# ────────────────────────────────────────────────────────
# Rate Limit Retry Wrapper
# ────────────────────────────────────────────────────────
@retry(
    stop=stop_after_attempt(6), 
    wait=wait_exponential(multiplier=2, min=4, max=60), 
    retry=retry_if_exception_type(ResourceExhausted), 
    reraise=True
)
async def _call_vertex_with_retry(prompt: str, config: GenerationConfig):
    """Calls Vertex AI and automatically pauses/retries if rate limits are hit."""
    _log.info("Attempting Vertex AI call...")
    return await hsn_model.generate_content_async(prompt, generation_config=config)


# ────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────

async def generate_hsn_codes(bids: List[Dict[str, str]]) -> Dict[str, Any]:
    """Generate HSN codes for a list of bid items.

    Parameters
    ----------
    bids : list[dict]
        Each dict must have ``bid_id`` (str) and ``item`` (str).

    Returns
    -------
    dict
        Structured response with ``status``, ``meta_data``, ``data``,
        ``error_code``, and ``error_messages``.
    """
    start_time = time.time()
    _log.info("HSN generation started – %d bids", len(bids))
    model_used = "gemini-2.5-flash"

    try:
        full_prompt = HSN_SYSTEM_PROMPT + "\n\n" + _build_user_prompt(bids)

        # Call Vertex AI (with automatic retry for 429 limits)
        _log.info("Sending HSN request to Vertex AI …")
        
        config = GenerationConfig(
            temperature=0.3,
            max_output_tokens=32768,
            response_mime_type="application/json", 
        )
        
        # Uses the retry wrapper instead of direct call
        response = await _call_vertex_with_retry(full_prompt, config)
        
        raw_text = response.text
        
        # Extract token usage
        usage = {}
        if hasattr(response, "usage_metadata"):
            usage["prompt_tokens"] = response.usage_metadata.prompt_token_count
            usage["completion_tokens"] = response.usage_metadata.candidates_token_count

        if not raw_text:
            _log.error("Vertex AI returned empty response for HSN generation")
            return _error_response(
                model_used=model_used,
                start_time=start_time,
                error_code="empty_response",
                messages=["Empty response from AI model"],
            )

        _log.info("Received Vertex AI response (%d chars)", len(raw_text))

        # Parse JSON (handles fences, truncation, repair)
        cleaned = _strip_markdown_fences(raw_text)
        try:
            parsed = parse_json_response(cleaned)
        except Exception:
            parsed = json.loads(cleaned)

        # ── Normalise structure ──────────────────────────────────
        if "results" not in parsed:
            if "hsn_codes" in parsed:
                parsed["results"] = [
                    {
                        "bid_id": item.get("bid_id") or item.get("bidId"),
                        "hsn": item.get("hsnCode") or item.get("hsn"),
                        "confidence": item.get("confidence", "medium"),
                        "reasoning": item.get("reasoning", ""),
                    }
                    for item in parsed["hsn_codes"]
                ]
            elif isinstance(parsed, list):
                parsed = {"results": parsed}
            else:
                raise ValueError(
                    "Invalid response structure — no 'results' or 'hsn_codes' field"
                )

        if not isinstance(parsed.get("results"), list):
            raise ValueError("'results' must be an array")

        # ── Validate & sanitise each result ──────────────────────
        for idx, item in enumerate(parsed["results"]):
            if "bid_id" not in item and "bidId" not in item:
                _log.warning("Result item %d missing bid_id", idx)
            # Normalise bidId → bid_id
            if "bidId" in item and "bid_id" not in item:
                item["bid_id"] = item.pop("bidId")

            # Sanitise HSN code
            item["hsn"] = _sanitise_hsn(item.get("hsn", ""), item.get("bid_id", idx))

        total_bids = len(parsed["results"])
        execution_time_ms = int((time.time() - start_time) * 1000)

        _log.info(
            "HSN generation complete – %d codes in %dms", total_bids, execution_time_ms
        )

        return {
            "status": "success",
            "meta_data": {
                "total_bids_processed": total_bids,
                "model_used": model_used,
                "execution_time_ms": execution_time_ms,
                "prompt_length": len(full_prompt),
                "response_length": len(raw_text),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
            "data": parsed,
            "error_code": "",
            "error_messages": [],
        }

    except json.JSONDecodeError as exc:
        _log.error("Failed to parse Vertex AI HSN response as JSON: %s", exc)
        return _error_response(
            model_used=model_used,
            start_time=start_time,
            error_code="json_parse_error",
            messages=[f"Failed to parse AI response: {exc}"],
        )

    except ValueError as exc:
        _log.error("HSN response validation failed: %s", exc)
        return _error_response(
            model_used=model_used,
            start_time=start_time,
            error_code="validation_error",
            messages=[str(exc)],
        )

    except Exception as exc:
        _log.exception("HSN generation failed")
        return _error_response(
            model_used="unknown",
            start_time=start_time,
            error_code="unexpected_error",
            messages=[f"HSN generation failed: {exc}"],
        )


def _error_response(
    *,
    model_used: str,
    start_time: float,
    error_code: str,
    messages: List[str],
) -> Dict[str, Any]:
    return {
        "status": "error",
        "meta_data": {
            "total_bids_processed": 0,
            "model_used": model_used,
            "execution_time_ms": int((time.time() - start_time) * 1000),
        },
        "data": {},
        "error_code": error_code,
        "error_messages": messages,
    }