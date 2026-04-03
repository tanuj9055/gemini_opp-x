"""
Gemini Generation wrapper using Vertex AI.

• Uses Vertex AI `Part.from_data` for inline files.
• Sends multimodal prompts (file handles + text) to Vertex AI Gemini.
• Returns the raw model output together with usage metadata.
• Includes timeout protection and automatic retries.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import vertexai
from vertexai.generative_models import GenerativeModel, Part, GenerationConfig
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, TooManyRequests, InvalidArgument

from app.config import get_settings
from app.logging_cfg import logger

_log = logger.getChild("gemini_client")

# Default timeout for Gemini API calls (seconds)
_GENERATION_TIMEOUT = 300

class QuotaExhaustedError(Exception):
    """Raised when the Gemini API quota is fully exhausted (hard limit, not transient)."""
    pass

_vertex_init_done = False

def _init_client():
    global _vertex_init_done
    if not _vertex_init_done:
        # Initialise Vertex AI using the project matching the service account
        vertexai.init(project="qistonpe-project-22810", location="us-central1")
        _vertex_init_done = True

def _guess_mime_type(file_path: Path) -> str:
    """Return a MIME type for *file_path* based on its extension."""
    import mimetypes
    mime, _ = mimetypes.guess_type(str(file_path))
    return mime or "application/pdf"

async def upload_file(file_path: Path, display_name: str | None = None) -> Any:
    """With Vertex AI, we don't 'upload' to a File API endpoint. We read the file inline into a Part object."""
    _init_client()
    name = display_name or file_path.name
    mime_type = _guess_mime_type(file_path)

    _log.info("Loading file → Vertex AI Part: %s (mime=%s)", name, mime_type)
    t0 = time.perf_counter()

    def _read_bytes():
        with open(file_path, "rb") as f:
            return f.read()

    data = await asyncio.to_thread(_read_bytes)
    part = Part.from_data(data=data, mime_type=mime_type)

    elapsed = time.perf_counter() - t0
    _log.info("File loaded: %s (%.2fs, %d bytes)", name, elapsed, len(data))
    return part

async def upload_files(file_paths: List[Path]) -> List[Any]:
    """Load multiple files concurrently."""
    tasks = [upload_file(fp) for fp in file_paths]
    return await asyncio.gather(*tasks)

async def delete_uploaded_file(file_ref: Any) -> None:
    """No-op for Vertex AI, as parts are inline."""
    pass

async def cleanup_files(file_refs: List[Any]) -> None:
    """No-op."""
    pass

async def generate(
    prompt: str,
    file_handles: List[Any] | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 65536,
    max_retries: int = 3,
    timeout: int = _GENERATION_TIMEOUT,
    response_mime_type: str = "application/json",
) -> Tuple[str, Dict[str, Any]]:
    """Send a multimodal prompt to Gemini and return ``(text, usage_meta)``."""
    _init_client()
    settings = get_settings()

    parts: list[Any] = []
    if file_handles:
        for fh in file_handles:
            parts.append(fh)
    parts.append(prompt)

    config = GenerationConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type=response_mime_type,
    )

    model = GenerativeModel(settings.gemini_model)

    _log.info(
        "Generating with Vertex AI model=%s | files=%d | prompt_chars=%d | timeout=%ds",
        settings.gemini_model,
        len(file_handles) if file_handles else 0,
        len(prompt),
        timeout,
    )
    t0 = time.perf_counter()

    for attempt in range(1, max_retries + 1):
        try:
            response = await asyncio.wait_for(
                model.generate_content_async(
                    parts,
                    generation_config=config,
                ),
                timeout=timeout,
            )
            elapsed = time.perf_counter() - t0
            
            text = response.text or ""

            usage: Dict[str, Any] = {
                "model": settings.gemini_model,
                "processing_time_seconds": round(elapsed, 3),
            }
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                um = response.usage_metadata
                usage["prompt_tokens"] = getattr(um, "prompt_token_count", None)
                usage["completion_tokens"] = getattr(um, "candidates_token_count", None)
                usage["total_tokens"] = getattr(um, "total_token_count", None)

            _log.info(
                "Generation complete (%.2fs) – tokens: prompt=%s / completion=%s",
                elapsed,
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
            )

            return text, usage

        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - t0
            _log.error(
                "Vertex AI call timed out after %.0fs on attempt %d/%d",
                elapsed, attempt, max_retries,
            )
            if attempt < max_retries:
                _log.info("Retrying after timeout...")
                continue
            raise TimeoutError(
                f"Vertex AI did not respond within {timeout}s after {max_retries} attempts."
            )

        except Exception as exc:
            error_msg = str(exc)
            lower_msg = error_msg.lower()

            is_quota_exhausted = isinstance(exc, ResourceExhausted) and ("quota" in lower_msg or "limit: 0" in lower_msg)
            
            if is_quota_exhausted:
                _log.error("Quota exhausted for model '%s'.", settings.gemini_model)
                raise QuotaExhaustedError(f"Vertex AI API quota exhausted: {error_msg}") from exc

            is_temporary = any([
                isinstance(exc, TooManyRequests),
                isinstance(exc, ServiceUnavailable),
                "503" in error_msg,
                "429" in error_msg,
                "temporarily unavailable" in lower_msg,
                "high demand" in lower_msg,
            ])
            
            if is_temporary and attempt < max_retries:
                wait_time = 2 ** attempt
                _log.warning(
                    "Attempt %d/%d failed (temporary): %s. Retrying in %ds...",
                    attempt, max_retries,
                    error_msg[:120],
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                continue
            
            raise


def _repair_truncated_json(text: str) -> str | None:
    """Try to repair JSON that was truncated mid-stream.

    Closes any open strings, arrays, and objects so that
    ``json.loads`` can parse the incomplete output.
    Returns the repaired string, or *None* if repair fails.
    """
    # Trim trailing comma / whitespace
    repaired = text.rstrip()
    if not repaired:
        return None

    # If we're inside an unterminated string, close it
    # Count unescaped quotes to check parity
    in_string = False
    i = 0
    while i < len(repaired):
        ch = repaired[i]
        if ch == '\\' and in_string:
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        i += 1
    if in_string:
        repaired += '"'

    # Remove any trailing comma
    repaired = repaired.rstrip()
    if repaired.endswith(','):
        repaired = repaired[:-1]

    # Count open brackets / braces and close them
    opens = []
    in_str = False
    j = 0
    while j < len(repaired):
        ch = repaired[j]
        if ch == '\\' and in_str:
            j += 2
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in ('{', '['):
                opens.append(ch)
            elif ch == '}' and opens and opens[-1] == '{':
                opens.pop()
            elif ch == ']' and opens and opens[-1] == '[':
                opens.pop()
        j += 1

    # Close remaining open brackets in reverse order
    for bracket in reversed(opens):
        repaired += ']' if bracket == '[' else '}'

    return repaired


def parse_json_response(raw: str) -> Dict[str, Any]:
    """Attempt to parse the model output as JSON.

    Handles common Gemini quirks such as markdown fences.
    Falls back to truncated-JSON repair when the output was cut short.
    """
    text = raw.strip()
    # Strip markdown code block wrappers if present
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3].rstrip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("JSON parse failure: %s  — attempting json_repair", exc)
        try:
            from json_repair import repair_json
            repaired_str = repair_json(text, return_objects=False)
            result = json.loads(repaired_str)
            _log.info("json_repair succeeded – parsed %d top-level keys", len(result) if isinstance(result, dict) else 0)
            return result
        except Exception as repair_exc:
            _log.warning("json_repair also failed: %s — trying manual truncation repair", repair_exc)

        # Last resort: manual truncation repair
        repaired = _repair_truncated_json(text)
        if repaired:
            try:
                result = json.loads(repaired)
                _log.info("Truncated-JSON repair succeeded (added %d chars)", len(repaired) - len(text))
                return result
            except json.JSONDecodeError:
                pass
        _log.error("JSON parse failure (unrecoverable): %s\nRaw output (first 500 chars):\n%s", exc, text[:500])
        return {"_parse_error": str(exc), "_raw_text": text}
