"""
Gemini Files API + generation wrapper.

• Uploads PDFs via the Gemini Files API (no chunking / RAG).
• Sends multimodal prompts (file handles + text) to Gemini.
• Returns the raw model output together with usage metadata.
• Includes timeout protection and automatic retries.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import types as genai_types

from app.config import get_settings
from app.logging_cfg import logger

_log = logger.getChild("gemini_client")

# Default timeout for Gemini API calls (seconds)
_GENERATION_TIMEOUT = 120


class QuotaExhaustedError(Exception):
    """Raised when the Gemini API quota is fully exhausted (hard limit, not transient)."""
    pass


# ────────────────────────────────────────────────────────
# Client singleton
# ────────────────────────────────────────────────────────

_client: Optional[genai.Client] = None
_client_key: Optional[str] = None  # track key to bust cache on change


def _get_client() -> genai.Client:
    """Lazy-init a google-genai ``Client``.  Re-creates if key changes."""
    global _client, _client_key
    settings = get_settings()
    if _client is None or _client_key != settings.google_api_key:
        _client = genai.Client(api_key=settings.google_api_key)
        _client_key = settings.google_api_key
        _log.info("Gemini client initialised (model=%s)", settings.gemini_model)
    return _client


# ────────────────────────────────────────────────────────
# File upload helpers
# ────────────────────────────────────────────────────────

async def upload_file(file_path: Path, display_name: str | None = None) -> Any:
    """Upload a single file to the Gemini Files API and return the file handle.

    Runs the blocking SDK call in a thread-pool so the event loop stays free.
    """
    client = _get_client()
    name = display_name or file_path.name

    _log.info("Uploading file → Gemini Files API: %s", name)
    t0 = time.perf_counter()

    uploaded = await asyncio.to_thread(
        client.files.upload,
        file=file_path,
        config=genai_types.UploadFileConfig(display_name=name),
    )

    elapsed = time.perf_counter() - t0
    _log.info("Upload complete: %s (%.2fs)", uploaded.name, elapsed)
    return uploaded


async def upload_files(file_paths: List[Path]) -> List[Any]:
    """Upload multiple files concurrently and return their handles."""
    tasks = [upload_file(fp) for fp in file_paths]
    return await asyncio.gather(*tasks)


async def delete_uploaded_file(file_ref: Any) -> None:
    """Best-effort cleanup of a previously uploaded file."""
    try:
        client = _get_client()
        await asyncio.to_thread(client.files.delete, name=file_ref.name)
        _log.debug("Deleted remote file: %s", file_ref.name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to delete remote file %s: %s", file_ref.name, exc)


async def cleanup_files(file_refs: List[Any]) -> None:
    """Delete a batch of uploaded files (fire-and-forget friendly)."""
    await asyncio.gather(*(delete_uploaded_file(f) for f in file_refs))


# ────────────────────────────────────────────────────────
# Generation
# ────────────────────────────────────────────────────────

async def generate(
    prompt: str,
    file_handles: List[Any] | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 32_768,
    max_retries: int = 3,
    timeout: int = _GENERATION_TIMEOUT,
) -> Tuple[str, Dict[str, Any]]:
    """Send a multimodal prompt to Gemini and return ``(text, usage_meta)``.

    ``file_handles`` are objects previously returned by ``upload_file``.
    
    Includes:
      - Hard timeout (default 120s) so requests never hang forever.
      - Retries on 503 / 429 / high-demand with exponential backoff.
    """
    settings = get_settings()
    client = _get_client()

    # Build content parts: files first, then the text instruction.
    parts: list[Any] = []
    if file_handles:
        for fh in file_handles:
            parts.append(fh)
    parts.append(prompt)

    config = genai_types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
    )

    _log.info(
        "Generating with model=%s | files=%d | prompt_chars=%d | timeout=%ds",
        settings.gemini_model,
        len(file_handles) if file_handles else 0,
        len(prompt),
        timeout,
    )
    t0 = time.perf_counter()

    # Retry logic for temporary API failures
    for attempt in range(1, max_retries + 1):
        try:
            # Wrap the blocking SDK call with a hard timeout
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model=settings.gemini_model,
                    contents=parts,
                    config=config,
                ),
                timeout=timeout,
            )
            elapsed = time.perf_counter() - t0
            
            # ── Extract text ──
            text = response.text or ""

            # ── Usage metadata ──
            usage: Dict[str, Any] = {
                "model": settings.gemini_model,
                "processing_time_seconds": round(elapsed, 3),
            }
            if response.usage_metadata:
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
                "Gemini call timed out after %.0fs on attempt %d/%d",
                elapsed, attempt, max_retries,
            )
            if attempt < max_retries:
                _log.info("Retrying after timeout...")
                continue
            raise TimeoutError(
                f"Gemini API did not respond within {timeout}s after {max_retries} attempts. "
                f"Try a faster model like gemini-2.5-flash."
            )

        except Exception as exc:
            error_msg = str(exc)
            lower_msg = error_msg.lower()

            # ── Hard quota exhaustion (limit: 0) — do NOT retry ──
            is_quota_exhausted = (
                "resource_exhausted" in lower_msg
                and ("limit: 0" in lower_msg or "quota exceeded" in lower_msg)
            )
            if is_quota_exhausted:
                # Extract the suggested retry delay if present
                retry_hint = ""
                if "please retry in" in lower_msg:
                    import re
                    m = re.search(r"please retry in ([\d.]+)s", lower_msg)
                    if m:
                        retry_hint = f" API suggests waiting {m.group(1)}s."
                _log.error(
                    "Quota exhausted for model '%s'. Free-tier limit reached.%s",
                    settings.gemini_model, retry_hint,
                )
                raise QuotaExhaustedError(
                    f"Gemini API quota exhausted for model '{settings.gemini_model}'. "
                    f"Your free-tier limit has been reached (limit: 0).{retry_hint} "
                    f"Either wait for the quota to reset, switch to a different model "
                    f"in .env (GEMINI_MODEL), or upgrade your Google AI plan."
                ) from exc

            # ── Transient rate-limit / server errors — retry ──
            is_temporary = any([
                "503" in error_msg or "service unavailable" in lower_msg,
                ("429" in error_msg or "rate limit" in lower_msg)
                and "limit: 0" not in lower_msg,
                "temporarily unavailable" in lower_msg,
                "high demand" in lower_msg,
            ])
            
            if is_temporary and attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s, 8s
                _log.warning(
                    "Attempt %d/%d failed (temporary): %s. Retrying in %ds...",
                    attempt, max_retries,
                    error_msg[:120],
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                continue
            
            # Handle permanent failures
            if "not found" in lower_msg or "not supported" in lower_msg:
                _log.error(
                    "Model '%s' is unavailable. Error: %s",
                    settings.gemini_model, error_msg,
                )
            
            raise


def parse_json_response(raw: str) -> Dict[str, Any]:
    """Attempt to parse the model output as JSON.

    Handles common Gemini quirks such as markdown fences.
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
        _log.error("JSON parse failure: %s\nRaw output (first 500 chars):\n%s", exc, text[:500])
        return {"_parse_error": str(exc), "_raw_text": text}
