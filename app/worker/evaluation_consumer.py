"""
RabbitMQ Evaluation Worker – consumes evaluation jobs and publishes results.

Queues
------
- **Consumes** from: ``rule_evaluation_jobs``   (durable queue)
- **Publishes** to:  ``rule_evaluation_results`` (durable queue)

Flow
----
After the NestJS server receives the classification response (checkable rules),
it publishes a job here with checkable rules + company profile.
This worker evaluates each checkable rule against the customer profile and
publishes passed/failed verdicts back to NestJS.

Message contract (inbound)
--------------------------
::

    {
      "job_id": "eval-abc123",
      "customer_id": "cust-001",
      "tender_id": "8481457",
      "checkable_rules": [ { ... CheckableRule ... }, ... ],
      "customer_profile": { ... structured company data ... }
    }

Result contract (outbound)
--------------------------
::

    {
      "job_id": "eval-abc123",
      "customer_id": "cust-001",
      "tender_id": "8481457",
      "status": "completed" | "failed",
      "evaluation_result": {
        "passed": [ { "rule_id": "...", "evidence": "..." }, ... ],
        "failed": [ { "rule_id": "...", "reason": "...", "evidence": "..." }, ... ]
      },
      "error": null | "error message",
      "processing_time_seconds": 15.3
    }

Usage
-----
Run as a standalone worker::

    python -m app.worker.evaluation_consumer

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
from app.agents.evaluation_agent import evaluate_rules

_log = logger.getChild("evaluation_worker")


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

async def _on_evaluation_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single evaluation job from RabbitMQ.

    Workflow:
      1. Decode & validate the JSON payload.
      2. Extract checkable_rules and customer_profile from the payload.
      3. Run evaluation via Gemini (evaluate_rules).
      4. Publish the result to ``rule_evaluation_results``.
      5. ACK on success, NACK (no requeue) on permanent failure.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_evaluation_results_queue
    tender_id = "UNKNOWN"
    t0 = time.perf_counter()

    try:
        raw_body = json.loads(message.body.decode())
        body = _unwrap_nestjs_message(raw_body)

        job_id = body.get("job_id", "UNKNOWN")
        customer_id = body.get("customer_id", "UNKNOWN")
        tender_id = body.get("tender_id", "UNKNOWN")
        checkable_rules = body.get("checkable_rules", [])
        customer_profile = body.get("customer_profile", {})

        _log.info(
            "📥 Evaluation job received — job_id=%s tender_id=%s "
            "customer_id=%s rule_count=%d",
            job_id, tender_id, customer_id, len(checkable_rules),
        )

        # ── Validate required fields ─────────────────────────────
        if not checkable_rules:
            _log.warning(
                "⚠️ [%s] No checkable rules provided — returning empty result",
                tender_id,
            )
            # Still produce a valid result with empty passed/failed
        if not customer_profile:
            raise ValueError(
                f"Missing or empty 'customer_profile' in evaluation job "
                f"for tender_id={tender_id}"
            )

        # ── Run evaluation ───────────────────────────────────────
        _log.info("[%s] Starting Gemini rule evaluation …", tender_id)
        try:
            evaluation_result = await evaluate_rules(checkable_rules, customer_profile)
        except Exception as exc:
            _log.error(
                "❌ [%s] Rule evaluation FAILED — error=%s",
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
            "evaluation_result": evaluation_result.model_dump(mode="json"),
            "error": None,
            "processing_time_seconds": round(elapsed, 2),
        }

        _log.info(
            "✅ [%s] Evaluation pipeline complete — passed=%d  "
            "failed=%d  elapsed=%.1fs",
            tender_id,
            len(evaluation_result.passed),
            len(evaluation_result.failed),
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
            "passed=%d  failed=%d",
            tender_id, results_queue_name,
            len(evaluation_result.passed),
            len(evaluation_result.failed),
        )

        await message.ack()
        _log.debug("[%s] Message ACKed", tender_id)

    except json.JSONDecodeError:
        _log.error(
            "❌ Non-JSON message on evaluation queue — rejecting: %s",
            message.body[:200],
        )
        await message.reject(requeue=False)

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _log.error(
            "❌ [%s] Evaluation job FAILED — elapsed=%.1fs\n%s",
            tender_id, elapsed, traceback.format_exc(),
        )

        # Publish error result so the caller knows
        try:
            error_result = {
                "job_id": locals().get("job_id", "UNKNOWN"),
                "customer_id": locals().get("customer_id", "UNKNOWN"),
                "tender_id": tender_id,
                "status": "failed",
                "evaluation_result": None,
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

async def start_evaluation_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare evaluation queues, and start consuming.

    This coroutine runs forever (or until cancelled). It is designed to be
    launched either:
      - As a background ``asyncio.Task`` inside the FastAPI lifespan, or
      - Directly from ``python -m app.worker.evaluation_consumer``.
    """
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url
    jobs_queue_name = settings.rabbitmq_evaluation_jobs_queue
    results_queue_name = settings.rabbitmq_evaluation_results_queue

    while True:
        try:
            _log.info(
                "🔌 Connecting evaluation worker to RabbitMQ at %s …", url
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
                    "✅ Evaluation worker ready — consuming from '%s', "
                    "publishing to '%s'",
                    jobs_queue_name,
                    results_queue_name,
                )

                async with jobs_queue.iterator() as queue_iter:
                    async for msg in queue_iter:
                        asyncio.create_task(
                            _on_evaluation_message(msg, channel)
                        )

        except asyncio.CancelledError:
            _log.info(
                "Evaluation worker task cancelled. Shutting down gracefully."
            )
            break
        except Exception as exc:
            _log.error(
                "RabbitMQ connection lost in Evaluation worker: %s. "
                "Reconnecting in 5s...",
                exc,
            )
            await asyncio.sleep(5)


# ────────────────────────────────────────────────────────
# Standalone entry point
# ────────────────────────────────────────────────────────

def run_evaluation_worker() -> None:
    """Synchronous entry point for ``python -m app.worker.evaluation_consumer``."""
    _log.info("🚀 Starting Evaluation Worker (standalone mode)")
    try:
        asyncio.run(start_evaluation_worker())
    except KeyboardInterrupt:
        _log.info("👋 Evaluation Worker stopped by user")


if __name__ == "__main__":
    run_evaluation_worker()
