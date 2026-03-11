"""
RabbitMQ HSN Worker – consumes HSN generation jobs and publishes results.

Queues
------
- **Consumes** from: ``hsn_generation_jobs``     (durable queue)
- **Publishes** to:  ``hsn_generation_results``   (durable queue)

Message contract (Publisher → Python)
-------------------------------------
Published to ``hsn_generation_jobs``::

    {
      "bid_id": "GEM/2024/B/12345",
      "item": "Laptop computer with 8GB RAM and 256GB SSD"
    }

Or batch::

    {
      "bids": [
        {"bid_id": "GEM/2024/B/001", "item": "Laptop computer"},
        {"bid_id": "GEM/2024/B/002", "item": "Office chairs"}
      ]
    }

Result contract (Python → Publisher)
-------------------------------------
Published to ``hsn_generation_results``::

    {
      "status": "success",
      "meta_data": {
        "total_bids_processed": 2,
        "model_used": "gemini-2.5-pro",
        "execution_time_ms": 1234
      },
      "data": {
        "results": [
          {
            "bid_id": "GEM/2024/B/001",
            "hsn": "847130",
            "confidence": "high",
            "reasoning": "Portable digital automatic data processing machine"
          }
        ]
      },
      "error_code": "",
      "error_messages": []
    }
"""

from __future__ import annotations

import asyncio
import json
import traceback

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.config import get_settings
from app.logging_cfg import logger
from app.services.hsn_generator import generate_hsn_codes

_log = logger.getChild("hsn_worker")


# ────────────────────────────────────────────────────────
# Message handler
# ────────────────────────────────────────────────────────

async def _on_hsn_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single HSN generation job from RabbitMQ.

    Accepts either:
      - A single item: ``{"bid_id": "...", "item": "..."}``
      - A batch:       ``{"bids": [{"bid_id": "...", "item": "..."}, ...]}``

    Publishes the structured result to ``hsn_generation_results``.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_hsn_results_queue

    try:
        body = json.loads(message.body.decode())

        # ── Normalise to a list of bids ──────────────────────────
        if "bids" in body and isinstance(body["bids"], list):
            bids = body["bids"]
        elif "bid_id" in body and "item" in body:
            bids = [{"bid_id": body["bid_id"], "item": body["item"]}]
        else:
            raise ValueError(
                "Invalid message format. Expected {bid_id, item} or {bids: [{bid_id, item}, ...]}"
            )

        _log.info(
            "📥 HSN job received – %d bid(s): %s",
            len(bids),
            ", ".join(b.get("bid_id", "?") for b in bids[:5]),
        )

        # ── Run HSN generation ───────────────────────────────────
        result = await generate_hsn_codes(bids)

        _log.info(
            "HSN generation done – status=%s  bids=%d",
            result.get("status"),
            result.get("meta_data", {}).get("total_bids_processed", 0),
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
            "📤 HSN result published to '%s' – status=%s",
            results_queue_name,
            result.get("status"),
        )

        await message.ack()

    except json.JSONDecodeError:
        _log.error("❌ Non-JSON message on HSN queue – rejecting: %s", message.body[:200])
        await message.reject(requeue=False)

    except Exception:
        _log.error("❌ HSN job failed:\n%s", traceback.format_exc())

        # Publish an error so the caller knows
        try:
            error_result = {
                "status": "failed",
                "error": traceback.format_exc(),
                "data": {},
                "error_code": "worker_error",
                "error_messages": [traceback.format_exc()],
            }
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(error_result, default=str).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=results_queue_name,
            )
        except Exception:
            _log.error("Failed to publish HSN error result")

        await message.reject(requeue=False)


# ────────────────────────────────────────────────────────
# Consumer entry point
# ────────────────────────────────────────────────────────

async def start_hsn_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare HSN queues, and start consuming.

    Runs forever (or until cancelled).  Launch either:
      - As a background ``asyncio.Task`` in the FastAPI lifespan, or
      - Directly via ``python -m app.worker.hsn_main``.
    """
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url
    jobs_queue_name = settings.rabbitmq_hsn_jobs_queue
    results_queue_name = settings.rabbitmq_hsn_results_queue

    _log.info("🔌 Connecting HSN worker to RabbitMQ at %s …", url)

    connection = await aio_pika.connect_robust(url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)

        # Declare both queues (idempotent)
        jobs_queue = await channel.declare_queue(jobs_queue_name, durable=True)
        await channel.declare_queue(results_queue_name, durable=True)

        _log.info(
            "✅ HSN worker ready – consuming from '%s', publishing to '%s'",
            jobs_queue_name,
            results_queue_name,
        )

        async with jobs_queue.iterator() as queue_iter:
            async for message in queue_iter:
                await _on_hsn_message(message, channel)


def run_hsn_worker() -> None:
    """Synchronous entry point for standalone mode."""
    _log.info("🚀 Starting HSN Worker (standalone mode)")
    try:
        asyncio.run(start_hsn_worker())
    except KeyboardInterrupt:
        _log.info("👋 HSN Worker stopped by user")


if __name__ == "__main__":
    run_hsn_worker()
