"""
RabbitMQ Filter Rules Worker – consumes filter rules jobs and publishes results.

Queues
------
- **Consumes** from: ``filter_rules``   (durable queue)
- **Publishes** to:  ``filter_rules_results`` (durable queue)

Flow
----
After the NestJS server determines eligibility criteria, it publishes a job here.
This worker filters the rules into verifiable and non-verifiable criteria
and publishes the result back.

Message contract (inbound)
--------------------------
::

    {
      "job_id": "flt-abc123",
      "customer_id": "cust-001",
      "payload": {
        "extracted_rules": [ ... ]
      }
    }

Result contract (outbound)
--------------------------
::

    {
      "job_id": "flt-abc123",
      "customer_id": "cust-001",
      "status": "completed" | "failed",
      "filter_result": {
        "verifiable_criteria": [ ... ],
        "non_verifiable_criteria": [ ... ]
      },
      "error": null | "error message",
      "processing_time_seconds": 12.5
    }

Usage
-----
Run as a standalone worker::

    python -m app.worker.filter_consumer

Or start alongside FastAPI via the ``lifespan`` hook in ``app.main``.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.config import get_settings
from app.logging_cfg import logger
from app.agents.filter_agent import filter_rules

_log = logger.getChild("filter_worker")


def _unwrap_nestjs_message(body: dict) -> dict:
    """Unwrap NestJS microservice message format if present."""
    if "pattern" in body and "data" in body and isinstance(body["data"], dict):
        _log.debug("Unwrapped NestJS message with pattern '%s'", body["pattern"])
        return body["data"]
    return body


async def _on_filter_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single filter rules job from RabbitMQ."""
    settings = get_settings()
    results_queue_name = settings.rabbitmq_filter_results_queue
    job_id = "UNKNOWN"
    customer_id = "UNKNOWN"
    t0 = time.perf_counter()

    try:
        raw_body = json.loads(message.body.decode())
        body = _unwrap_nestjs_message(raw_body)

        job_id = body.get("job_id", "UNKNOWN")
        customer_id = body.get("customer_id", "UNKNOWN")
        payload = body.get("payload", {})
        extracted_rules = payload.get("extracted_rules", [])

        _log.info(
            "📥 Filter rules job received — job_id=%s customer_id=%s rule_count=%d",
            job_id, customer_id, len(extracted_rules),
        )

        if not extracted_rules:
            _log.warning(
                "⚠️ [%s] Empty 'extracted_rules' in payload — proceeding anyway",
                job_id,
            )

        _log.info("[%s] Starting Gemini filter agent …", job_id)
        try:
            # The filter_rules agent returns a FilterRulesResponse
            filter_response = await filter_rules(extracted_rules)
            # Convert the Pydantic model to a dict for JSON serialization
            filter_result = filter_response.model_dump()
        except Exception as exc:
            _log.error(
                "❌ [%s] Rule filtering FAILED — error=%s",
                job_id, exc,
                exc_info=True,
            )
            raise

        elapsed = time.perf_counter() - t0
        result = {
            "job_id": job_id,
            "customer_id": customer_id,
            "status": "completed",
            "filter_result": filter_result,
            "processing_time_seconds": round(elapsed, 2),
            "error": None,
        }

        result_body = json.dumps(result, ensure_ascii=False).encode()

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
            "📤 Result published for filter job %s — total_time=%.2fs\n"
            "   └─ Verifiable: %d | Non-verifiable: %d",
            job_id,
            elapsed,
            len(filter_result.get("verifiable_criteria", [])),
            len(filter_result.get("non_verifiable_criteria", [])),
        )

        await message.ack()

    except json.JSONDecodeError:
        _log.error("❌ Non-JSON message received for filter rules — rejecting")
        await message.reject(requeue=False)

    except Exception as exc:
        _log.error(
            "❌ Filter rules job %s failed with unhandled error:\n%s",
            job_id, traceback.format_exc(),
        )
        try:
            elapsed = time.perf_counter() - t0
            error_result = {
                "job_id": job_id,
                "customer_id": customer_id,
                "status": "failed",
                "filter_result": None,
                "error": str(exc),
                "processing_time_seconds": round(elapsed, 2),
            }
            await channel.declare_queue(results_queue_name, durable=True)
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(error_result, ensure_ascii=False).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=results_queue_name,
            )
        except Exception:
            _log.error("Failed to publish error result for filter job %s", job_id)

        await message.reject(requeue=False)


async def start_filter_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare queues, and start consuming filter jobs."""
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url
    jobs_queue_name = settings.rabbitmq_filter_jobs_queue
    results_queue_name = settings.rabbitmq_filter_results_queue

    _log.info("🔌 Connecting Filter Rules worker to RabbitMQ at %s …", url)

    connection = await aio_pika.connect_robust(url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)

        jobs_queue = await channel.declare_queue(jobs_queue_name, durable=True)
        await channel.declare_queue(results_queue_name, durable=True)

        _log.info(
            "✅ Filter Rules Worker ready — consuming from '%s', publishing to '%s'",
            jobs_queue_name, results_queue_name,
        )

        async with jobs_queue.iterator() as queue_iter:
            async for message in queue_iter:
                await _on_filter_message(message, channel)


def run_filter_worker() -> None:
    """Synchronous entry point for standalone consumer execution."""
    _log.info("🚀 Starting Filter Rules AI Worker (standalone mode)")
    try:
        asyncio.run(start_filter_worker())
    except KeyboardInterrupt:
        _log.info("👋 Filter Rules Worker stopped by user")


if __name__ == "__main__":
    run_filter_worker()
