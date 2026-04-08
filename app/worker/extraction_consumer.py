"""
RabbitMQ Extraction Worker – consumes tender extraction jobs and publishes results.

Queues
------
- **Consumes** from: ``tender_extraction_jobs``   (durable queue)
- **Publishes** to:  ``tender_extraction_results`` (durable queue)

The worker uses `aio_pika` for async RabbitMQ communication and delegates
the actual extraction to :func:`app.services.rule_extractor.extract_rules`.

Message contract (inbound)
--------------------------
::

    {
      "tender_id": "8481457",
      "tender_document_url": "s3://bucket/path/tender.pdf"
    }

Result contract (outbound)
--------------------------
::

    {
      "tender_id": "8481457",
      "status": "completed" | "failed",
      "extraction_result": { ... TenderExtractionResult ... },
      "error": null | "error message",
      "processing_time_seconds": 42.5
    }

Usage
-----
Run as a standalone worker::

    python -m app.worker.extraction_consumer

Or start alongside FastAPI via the ``lifespan`` hook in ``app.main``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.config import get_settings
from app.logging_cfg import logger
from app.services.rule_extractor import extract_rules
from app.services.s3_client import download_file

_log = logger.getChild("extraction_worker")


# ────────────────────────────────────────────────────────
# NestJS message unwrapper (reuse pattern from pdf_consumer)
# ────────────────────────────────────────────────────────

def _unwrap_nestjs_message(body: dict) -> dict:
    """Unwrap NestJS microservice message format if present."""
    if "pattern" in body and "data" in body and isinstance(body["data"], dict):
        _log.debug("Unwrapped NestJS message with pattern '%s'", body["pattern"])
        return body["data"]
    return body


# ────────────────────────────────────────────────────────
# Message handler
# ────────────────────────────────────────────────────────

async def _on_extraction_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single tender extraction job from RabbitMQ.

    Workflow:
      1. Decode & validate the JSON payload.
      2. Download the tender PDF.
      3. Run rule extraction via Gemini.
      4. Publish the result to ``tender_extraction_results``.
      5. ACK on success, NACK (no requeue) on permanent failure.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_extraction_results_queue
    tender_id = "UNKNOWN"
    t0 = time.perf_counter()

    try:
        raw_body = json.loads(message.body.decode())
        body = _unwrap_nestjs_message(raw_body)

        tender_id = body.get("tender_id", "UNKNOWN")
        tender_document_url = body.get("tender_document_url", "")

        _log.info(
            "📥 Extraction job received — tender_id=%s  url=%s",
            tender_id,
            tender_document_url[:120] if tender_document_url else "(empty)",
        )

        # ── Validate required fields ─────────────────────────────
        if not tender_document_url:
            raise ValueError(
                f"Missing 'tender_document_url' in extraction job for tender_id={tender_id}"
            )

        # ── Download tender PDF ──────────────────────────────────
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"gem_extract_{tender_id}_"))
        _log.debug(
            "[%s] Downloading tender PDF to temp dir: %s",
            tender_id, tmp_dir,
        )

        try:
            pdf_path = await download_file(tender_document_url, tmp_dir)
            _log.info(
                "[%s] Tender PDF downloaded — path=%s  size=%.1f KB",
                tender_id,
                pdf_path.name,
                pdf_path.stat().st_size / 1024,
            )
        except Exception as exc:
            _log.error(
                "❌ [%s] Tender PDF download FAILED — url=%s  error=%s",
                tender_id, tender_document_url, exc,
            )
            raise RuntimeError(f"Tender PDF download failed: {exc}") from exc

        # ── Run rule extraction ──────────────────────────────────
        _log.info("[%s] Starting Gemini rule extraction …", tender_id)
        try:
            extraction_result = await extract_rules(pdf_path)
        except Exception as exc:
            _log.error(
                "❌ [%s] Rule extraction FAILED — error=%s",
                tender_id, exc,
                exc_info=True,
            )
            raise

        # ── Build success result ─────────────────────────────────
        elapsed = time.perf_counter() - t0
        result = {
            "tender_id": tender_id,
            "status": "completed",
            "extraction_result": extraction_result.model_dump(mode="json"),
            "error": None,
            "processing_time_seconds": round(elapsed, 2),
        }

        _log.info(
            "✅ [%s] Extraction pipeline complete — rules=%d  elapsed=%.1fs",
            tender_id,
            len(extraction_result.rules),
            elapsed,
        )

        # ── Publish result ───────────────────────────────────────
        result_body = json.dumps(result, ensure_ascii=False, default=str).encode()
        await channel.declare_queue(results_queue_name, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=result_body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=results_queue_name,
        )

        _log.info(
            "📤 [%s] Result published to '%s' — status=completed  rules=%d",
            tender_id, results_queue_name, len(extraction_result.rules),
        )

        await message.ack()
        _log.debug("[%s] Message ACKed", tender_id)

        # ── Cleanup ──────────────────────────────────────────────
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _log.debug("[%s] Temp dir cleaned up: %s", tender_id, tmp_dir)
        except Exception:
            _log.warning("[%s] Temp dir cleanup failed: %s", tender_id, tmp_dir)

    except json.JSONDecodeError:
        _log.error(
            "❌ Non-JSON message on extraction queue — rejecting: %s",
            message.body[:200],
        )
        await message.reject(requeue=False)

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ [%s] Extraction job FAILED — elapsed=%.1fs\n%s",
            tender_id, elapsed, traceback.format_exc(),
        )

        # Publish error result so the caller knows
        try:
            error_result = {
                "tender_id": tender_id,
                "status": "failed",
                "extraction_result": None,
                "error": str(exc),
                "processing_time_seconds": round(elapsed, 2),
            }
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(error_result, default=str).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=results_queue_name,
            )
            _log.info(
                "📤 [%s] Error result published to '%s'",
                tender_id, results_queue_name,
            )
        except Exception:
            _log.error(
                "❌ [%s] Failed to publish error result", tender_id,
            )

        await message.reject(requeue=False)
        _log.debug("[%s] Message NACKed (no requeue)", tender_id)


# ────────────────────────────────────────────────────────
# Consumer entry point
# ────────────────────────────────────────────────────────

async def start_extraction_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare extraction queues, and start consuming.

    This coroutine runs forever (or until cancelled). It is designed to be
    launched either:
      - As a background ``asyncio.Task`` inside the FastAPI lifespan, or
      - Directly from ``python -m app.worker.extraction_consumer``.
    """
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url
    jobs_queue_name = settings.rabbitmq_extraction_jobs_queue
    results_queue_name = settings.rabbitmq_extraction_results_queue

    _log.info("🔌 Connecting extraction worker to RabbitMQ at %s …", url)

    connection = await aio_pika.connect_robust(url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)

        # Declare both queues (idempotent)
        jobs_queue = await channel.declare_queue(jobs_queue_name, durable=True)
        await channel.declare_queue(results_queue_name, durable=True)

        _log.info(
            "✅ Extraction worker ready — consuming from '%s', publishing to '%s'",
            jobs_queue_name,
            results_queue_name,
        )

        async with jobs_queue.iterator() as queue_iter:
            async for msg in queue_iter:
                await _on_extraction_message(msg, channel)


# ────────────────────────────────────────────────────────
# Standalone entry point
# ────────────────────────────────────────────────────────

def run_extraction_worker() -> None:
    """Synchronous entry point for ``python -m app.worker.extraction_consumer``."""
    _log.info("🚀 Starting Extraction Worker (standalone mode)")
    try:
        asyncio.run(start_extraction_worker())
    except KeyboardInterrupt:
        _log.info("👋 Extraction Worker stopped by user")


if __name__ == "__main__":
    run_extraction_worker()
