"""
RabbitMQ Analysis Worker – consumes tender analysis jobs and publishes results.

Queues
------
- **Consumes** from: ``tender_analysis_jobs``   (durable queue)
- **Publishes** to:  ``tender_analysis_results`` (durable queue)

The worker uses `aio_pika` for async RabbitMQ communication and delegates
the actual analysis to :func:`app.agents.bid_analyzer.analyze_bid`.

Message contract (inbound)
--------------------------
::

    {
      "tender_id": "8481457",
      "job_id": "...",
      "customer_id": "...",
      "ocr_text": "Text of the tender...",
      "embedded_links_ocr": [{"link": "url", "context": "text"}]
    }

Result contract (outbound)
--------------------------
::

    {
      "tender_id": "8481457",
      "status": "completed" | "failed",
      "analysis_result": { ... TenderAnalysisResult ... },
      "error": null | "error message",
      "processing_time_seconds": 42.5
    }
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
from app.agents.bid_analyzer import analyze_bid

_log = logger.getChild("analysis_worker")


def _unwrap_nestjs_message(body: dict) -> dict:
    """Unwrap NestJS microservice message format if present."""
    if "pattern" in body and "data" in body and isinstance(body["data"], dict):
        _log.debug("Unwrapped NestJS message with pattern '%s'", body["pattern"])
        return body["data"]
    return body

async def _on_analysis_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single tender analysis job from RabbitMQ.

    Workflow:
      1. Decode & validate the JSON payload (OCR text).
      2. Run bid analysis via Gemini using the OCR data.
      3. Publish the result to ``tender_analysis_results``.
      4. ACK on success, NACK (no requeue) on permanent failure.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_analysis_results_queue
    tender_id = "UNKNOWN"
    t0 = time.perf_counter()

    try:
        raw_body = json.loads(message.body.decode())
        body = _unwrap_nestjs_message(raw_body)

        job_id = body.get("job_id", "")
        customer_id = body.get("customer_id", "UNKNOWN")
        tender_id = body.get("tender_id", "UNKNOWN")
        ocr_text = body.get("ocr_text", "")
        embedded_links_ocr = body.get("embedded_links_ocr", [])

        _log.info(
            "📥 Analysis job received — job_id=%s tender_id=%s customer_id=%s ocr_text_len=%d links_count=%d",
            job_id, tender_id, customer_id, len(ocr_text), len(embedded_links_ocr)
        )

        if not ocr_text:
            raise ValueError(
                f"Missing 'ocr_text' in analysis job for tender_id={tender_id}"
            )

        _log.info("[%s] Starting Gemini bid analysis …", tender_id)
        try:
            analysis_result = await analyze_bid(ocr_text, embedded_links_ocr)
        except Exception as exc:
            _log.error(
                "❌ [%s] Bid analysis FAILED — error=%s",
                tender_id, exc,
                exc_info=True,
            )
            raise

        elapsed = time.perf_counter() - t0
        result_payload = {
            "job_id": job_id,
            "customer_id": customer_id,
            "tender_id": tender_id,
            "status": "completed",
            "analysis_result": analysis_result.model_dump(mode="json"),
            "error": None,
            "processing_time_seconds": round(elapsed, 2),
        }

        # Publish success result
        exchange = channel.default_exchange
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(result_payload).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=results_queue_name,
        )
        _log.info(
            "📤 Analysis result published — tender_id=%s queue=%s",
            tender_id, results_queue_name,
        )

        await message.ack()

    except Exception as overall_exc:
        _log.error(
            "❌ Fatal error processing analysis job [tender_id=%s] — %s",
            tender_id,
            overall_exc,
            exc_info=True,
        )
        elapsed = time.perf_counter() - t0
        error_payload = {
            "job_id": body.get("job_id", "") if 'body' in locals() else "",
            "customer_id": body.get("customer_id", "") if 'body' in locals() else "",
            "tender_id": tender_id,
            "status": "failed",
            "analysis_result": None,
            "error": str(overall_exc),
            "processing_time_seconds": round(elapsed, 2),
            "traceback": traceback.format_exc(),
        }
        try:
            exchange = channel.default_exchange
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(error_payload).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=settings.rabbitmq_analysis_results_queue,
            )
            _log.info("📤 Analysis error result published — tender_id=%s", tender_id)
        except Exception as pub_exc:
            _log.critical(
                "🔥 FAILED to publish error result [tender_id=%s] — %s",
                tender_id, pub_exc,
                exc_info=True,
            )

        _log.warning("Dropping analysis message (NACK without requeue) — tender_id=%s", tender_id)
        await message.nack(requeue=False)

    finally:
        if 'tmp_dir' in locals() and tmp_dir.exists():
            _log.debug("Cleaning up temp directory: %s", tmp_dir)
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def start_analysis_worker(rabbitmq_url: str) -> None:
    """Connect to RabbitMQ and start consuming analysis jobs."""
    settings = get_settings()
    queue_name = settings.rabbitmq_analysis_jobs_queue

    while True:
        try:
            _log.info("Connecting to RabbitMQ for Analysis Worker at %s", rabbitmq_url)
            connection = await aio_pika.connect_robust(rabbitmq_url)

            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=1)

                queue = await channel.declare_queue(queue_name, durable=True)

                _log.info("✅ Analysis worker ready. Listening on queue '%s'", queue_name)

                async with queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        asyncio.create_task(_on_analysis_message(message, channel))

        except asyncio.CancelledError:
            _log.info("Analysis worker task cancelled. Shutting down gracefully.")
            break
        except Exception as exc:
            _log.error(
                "RabbitMQ connection lost in Analysis worker: %s. Reconnecting in 5s...", exc
            )
            await asyncio.sleep(5)

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    loop = asyncio.get_event_loop()
    settings_ = get_settings()
    try:
        loop.run_until_complete(start_analysis_worker(settings_.rabbitmq_url))
    except KeyboardInterrupt:
        _log.info("Analysis worker stopped by user (Ctrl+C).")
