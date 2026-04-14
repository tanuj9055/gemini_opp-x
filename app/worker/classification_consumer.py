"""
RabbitMQ Classification Worker – consumes classification jobs and publishes results.

Queues
------
- **Consumes** from: ``rule_classification_jobs``   (durable queue)
- **Publishes** to:  ``rule_classification_results`` (durable queue)

Flow
----
After the NestJS server receives the rule extraction response from the user,
it publishes a job here with extracted rules + company profile.
This worker classifies rules into checkable vs non-checkable and publishes
the result back so NestJS can forward checkable rules to the evaluation agent.

Message contract (inbound)
--------------------------
::

    {
      "job_id": "cls-abc123",
      "customer_id": "cust-001",
      "tender_id": "8481457",
      "rules": [ { ... ExtractedRule ... }, ... ],
      "customer_profile": { ... structured company data ... }
    }

Result contract (outbound)
--------------------------
::

    {
      "job_id": "cls-abc123",
      "customer_id": "cust-001",
      "tender_id": "8481457",
      "status": "completed" | "failed",
      "classification_result": {
        "checkable_rules": [ ... ],
        "non_checkable_rules": [ ... ]
      },
      "error": null | "error message",
      "processing_time_seconds": 12.5
    }

Usage
-----
Run as a standalone worker::

    python -m app.worker.classification_consumer

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
from app.agents.classification_agent import classify_rules

_log = logger.getChild("classification_worker")


# ────────────────────────────────────────────────────────
# NestJS message unwrapper (reuse pattern from other consumers)
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

async def _on_classification_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single classification job from RabbitMQ.

    Workflow:
      1. Decode & validate the JSON payload.
      2. Extract rules list and customer_profile from the payload.
      3. Run classification via Gemini (classify_rules).
      4. Publish the result to ``rule_classification_results``.
      5. ACK on success, NACK (no requeue) on permanent failure.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_classification_results_queue
    tender_id = "UNKNOWN"
    t0 = time.perf_counter()

    try:
        raw_body = json.loads(message.body.decode())
        body = _unwrap_nestjs_message(raw_body)

        job_id = body.get("job_id", "UNKNOWN")
        customer_id = body.get("customer_id", "UNKNOWN")
        tender_id = body.get("tender_id", "UNKNOWN")
        rules = body.get("rules", [])
        customer_profile = body.get("customer_profile", {})

        _log.info(
            "📥 Classification job received — job_id=%s tender_id=%s "
            "customer_id=%s rule_count=%d",
            job_id, tender_id, customer_id, len(rules),
        )

        # ── Validate required fields ─────────────────────────────
        # rules can be empty for low-eligibility bids — do not throw
        if not rules:
            _log.warning(
                "⚠️ [%s] Empty 'rules' in classification job — low-eligibility bid, proceeding",
                tender_id,
            )
        if not customer_profile:
            raise ValueError(
                f"Missing or empty 'customer_profile' in classification job "
                f"for tender_id={tender_id}"
            )

        # ── Run classification ───────────────────────────────────
        _log.info("[%s] Starting Gemini rule classification …", tender_id)
        try:
            classification_result = await classify_rules(rules, customer_profile)
        except Exception as exc:
            _log.error(
                "❌ [%s] Rule classification FAILED — error=%s",
                tender_id, exc,
                exc_info=True,
            )
            raise

        # ── Build success result ─────────────────────────────────
        elapsed = time.perf_counter() - t0
        result = {
            "job_id": job_id,
            "customer_id": customer_id,
            "tender_id": tender_id,
            "status": "completed",
            "classification_result": classification_result.model_dump(mode="json"),
            "error": None,
            "processing_time_seconds": round(elapsed, 2),
        }

        _log.info(
            "✅ [%s] Classification pipeline complete — checkable=%d  "
            "non_checkable=%d  elapsed=%.1fs",
            tender_id,
            len(classification_result.checkable_rules),
            len(classification_result.non_checkable_rules),
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
            "📤 [%s] Result published to '%s' — status=completed  "
            "checkable=%d  non_checkable=%d",
            tender_id, results_queue_name,
            len(classification_result.checkable_rules),
            len(classification_result.non_checkable_rules),
        )

        await message.ack()
        _log.debug("[%s] Message ACKed", tender_id)

    except json.JSONDecodeError:
        _log.error(
            "❌ Non-JSON message on classification queue — rejecting: %s",
            message.body[:200],
        )
        await message.reject(requeue=False)

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ [%s] Classification job FAILED — elapsed=%.1fs\n%s",
            tender_id, elapsed, traceback.format_exc(),
        )

        # Publish error result so the caller knows
        try:
            error_result = {
                "job_id": locals().get("job_id", "UNKNOWN"),
                "customer_id": locals().get("customer_id", "UNKNOWN"),
                "tender_id": tender_id,
                "status": "failed",
                "classification_result": None,
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

async def start_classification_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare classification queues, and start consuming.

    This coroutine runs forever (or until cancelled). It is designed to be
    launched either:
      - As a background ``asyncio.Task`` inside the FastAPI lifespan, or
      - Directly from ``python -m app.worker.classification_consumer``.
    """
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url
    jobs_queue_name = settings.rabbitmq_classification_jobs_queue
    results_queue_name = settings.rabbitmq_classification_results_queue

    while True:
        try:
            _log.info(
                "🔌 Connecting classification worker to RabbitMQ at %s …", url
            )
            connection = await aio_pika.connect_robust(url)

            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=1)

                # Declare both queues (idempotent)
                jobs_queue = await channel.declare_queue(
                    jobs_queue_name, durable=True
                )
                await channel.declare_queue(results_queue_name, durable=True)

                _log.info(
                    "✅ Classification worker ready — consuming from '%s', "
                    "publishing to '%s'",
                    jobs_queue_name,
                    results_queue_name,
                )

                async with jobs_queue.iterator() as queue_iter:
                    async for msg in queue_iter:
                        asyncio.create_task(
                            _on_classification_message(msg, channel)
                        )

        except asyncio.CancelledError:
            _log.info(
                "Classification worker task cancelled. Shutting down gracefully."
            )
            break
        except Exception as exc:
            _log.error(
                "RabbitMQ connection lost in Classification worker: %s. "
                "Reconnecting in 5s...",
                exc,
            )
            await asyncio.sleep(5)


# ────────────────────────────────────────────────────────
# Standalone entry point
# ────────────────────────────────────────────────────────

def run_classification_worker() -> None:
    """Synchronous entry point for ``python -m app.worker.classification_consumer``."""
    _log.info("🚀 Starting Classification Worker (standalone mode)")
    try:
        asyncio.run(start_classification_worker())
    except KeyboardInterrupt:
        _log.info("👋 Classification Worker stopped by user")


if __name__ == "__main__":
    run_classification_worker()
