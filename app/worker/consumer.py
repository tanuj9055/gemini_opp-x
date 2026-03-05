"""
RabbitMQ AI Worker – consumes bid evaluation jobs and publishes results.

Queues
------
- **Consumes** from: ``bid_evaluation_jobs``   (durable queue)
- **Publishes** to:  ``bid_evaluation_results`` (durable queue)

The worker uses `aio_pika` for async RabbitMQ communication and delegates
the actual AI pipeline to :func:`app.worker.job_processor.process_evaluation_job`.

Usage
-----
Run as a standalone worker (no HTTP server needed)::

    python -m app.worker.main

Or start alongside FastAPI via the ``lifespan`` hook in ``app.main``.
"""

from __future__ import annotations

import asyncio
import json
import traceback

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.config import get_settings
from app.logging_cfg import logger
from app.worker.job_processor import process_evaluation_job

_log = logger.getChild("rmq_worker")

# Queue names (shared convention with NestJS backend)
JOBS_QUEUE = "bid_evaluation_jobs"
RESULTS_QUEUE = "bid_evaluation_results"


# ────────────────────────────────────────────────────────
# Message handler
# ────────────────────────────────────────────────────────

async def _on_job_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single job message from RabbitMQ.

    Workflow:
      1. Decode & validate the JSON payload.
      2. Run the full evaluation pipeline.
      3. Publish the result to ``bid_evaluation_results``.
      4. ACK the original message on success or NACK (no requeue) on
         permanent failure.
    """
    job_id = "UNKNOWN"
    try:
        body = json.loads(message.body.decode())
        job_id = body.get("job_id", "UNKNOWN")

        _log.info(
            "📥 Received job %s – bid_id=%s  vendors=%d",
            job_id,
            body.get("bid_id", "?"),
            len(body.get("vendors", [])),
        )

        # ── Run the pipeline ─────────────────────────────────────
        result = await process_evaluation_job(body)

        # ── Publish result ───────────────────────────────────────
        result_body = json.dumps(result, ensure_ascii=False, default=str).encode()

        # Declare the results queue (idempotent) and publish
        results_queue = await channel.declare_queue(
            RESULTS_QUEUE, durable=True,
        )
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=result_body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=RESULTS_QUEUE,
        )

        _log.info(
            "📤 Result published for job %s – status=%s",
            job_id, result.get("status", "?"),
        )

        # ACK after successful processing + publishing
        await message.ack()

    except json.JSONDecodeError:
        _log.error("❌ Non-JSON message received – rejecting: %s", message.body[:200])
        await message.reject(requeue=False)

    except Exception:
        _log.error(
            "❌ Job %s failed with unhandled error:\n%s",
            job_id, traceback.format_exc(),
        )
        # Publish an error result so NestJS knows the job failed
        try:
            error_result = {
                "job_id": job_id,
                "status": "failed",
                "error": traceback.format_exc(),
                "vendor_results": [],
            }
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(error_result, default=str).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=RESULTS_QUEUE,
            )
        except Exception:
            _log.error("Failed to publish error result for job %s", job_id)

        await message.reject(requeue=False)


# ────────────────────────────────────────────────────────
# Consumer entry point
# ────────────────────────────────────────────────────────

async def start_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare queues, and start consuming jobs.

    This coroutine runs forever (or until cancelled).  It is designed to be
    launched either:
      - As a background ``asyncio.Task`` inside the FastAPI lifespan, or
      - Directly from ``python -m app.worker.main``.
    """
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url

    _log.info("🔌 Connecting worker to RabbitMQ at %s …", url)

    connection = await aio_pika.connect_robust(url)

    async with connection:
        channel = await connection.channel()

        # Only process one job at a time per worker (back-pressure)
        await channel.set_qos(prefetch_count=1)

        # Declare both queues (idempotent – safe if they already exist)
        jobs_queue = await channel.declare_queue(JOBS_QUEUE, durable=True)
        await channel.declare_queue(RESULTS_QUEUE, durable=True)

        _log.info(
            "✅ Worker ready – consuming from '%s', publishing to '%s'",
            JOBS_QUEUE, RESULTS_QUEUE,
        )

        # Start consuming; pass channel so handler can publish results
        async with jobs_queue.iterator() as queue_iter:
            async for message in queue_iter:
                await _on_job_message(message, channel)


# ────────────────────────────────────────────────────────
# Standalone entry point
# ────────────────────────────────────────────────────────

def run_worker() -> None:
    """Synchronous entry point for ``python -m app.worker.main``."""
    _log.info("🚀 Starting AI Worker (standalone mode)")
    try:
        asyncio.run(start_worker())
    except KeyboardInterrupt:
        _log.info("👋 Worker stopped by user")


if __name__ == "__main__":
    run_worker()
