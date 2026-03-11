"""
RabbitMQ HSN Worker – consumes HSN generation jobs and publishes results.

Queues
------
- **Consumes** from: ``hsn_requests_queue``       (durable queue)
- **Publishes** to:  ``hsn_generation_results``    (durable queue)

Message contract (NestJS → Python)
-----------------------------------
NestJS ``ClientProxy.emit()`` publishes to ``hsn_requests_queue``::

    {
      "pattern": "hsn_generation_request",
      "data": {
        "batchId": "hsn_batch_1710000000000_1",
        "bids": [
          {"bidId": 1, "bidNumber": "GEM/2024/B/001", "items": "Laptop computer"},
          {"bidId": 2, "bidNumber": "GEM/2024/B/002", "items": "Office chairs"}
        ]
      }
    }

Result contract (Python → NestJS)
----------------------------------
Published to ``hsn_generation_results`` in NestJS microservice format::

    {
      "pattern": "hsn_generation_result",
      "data": {
        "status": "success",
        "meta_data": {
          "total_bids_processed": 2,
          "model_used": "gemini-2.5-pro",
          "execution_time_ms": 1234
        },
        "data": {
          "results": [
            {
              "bid_id": "1",
              "bidId": 1,
              "hsn": "847130",
              "confidence": "high",
              "reasoning": "Portable digital automatic data processing machine"
            }
          ]
        },
        "error_code": "",
        "error_messages": []
      }
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

def _unwrap_nestjs_message(body: dict) -> dict:
    """Unwrap NestJS microservice message format.

    NestJS ``ClientProxy.emit()`` wraps messages as::

        {"pattern": "<event_name>", "data": {<actual payload>}}

    This helper returns the inner ``data`` dict, or the original
    body if it isn't in NestJS format.
    """
    if "pattern" in body and "data" in body and isinstance(body["data"], dict):
        _log.info("Unwrapped NestJS message with pattern '%s'", body["pattern"])
        return body["data"]
    return body


def _map_nestjs_bids_to_python(bids: list) -> tuple:
    """Map NestJS bid fields to Python format and build a reverse lookup.

    NestJS sends ``{bidId, bidNumber, items}``.
    Python expects ``{bid_id, item}``.

    Returns (mapped_bids, id_lookup) where *id_lookup* maps the
    string ``bid_id`` back to the original numeric ``bidId``.
    """
    mapped = []
    id_lookup: dict = {}  # str(bid_id) → original bidId value

    for b in bids:
        # Accept both NestJS (bidId/items) and Python (bid_id/item) field names
        bid_id = b.get("bidId") or b.get("bid_id") or b.get("bidNumber", "")
        item = b.get("items") or b.get("item", "")

        str_bid_id = str(bid_id)
        id_lookup[str_bid_id] = b.get("bidId") or b.get("bid_id")

        mapped.append({"bid_id": str_bid_id, "item": item})

    return mapped, id_lookup


def _map_results_to_nestjs(result: dict, id_lookup: dict) -> dict:
    """Convert Python result fields back to NestJS format.

    Maps ``bid_id`` → ``bidId`` (original numeric value) in each result item
    so NestJS can match them to database records.
    """
    results_list = result.get("data", {}).get("results", [])
    for item in results_list:
        str_id = str(item.get("bid_id", ""))
        original_id = id_lookup.get(str_id, item.get("bid_id"))
        item["bidId"] = original_id
        # Keep bid_id as well for backwards compatibility
    return result


async def _on_hsn_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single HSN generation job from RabbitMQ.

    Accepts NestJS microservice format::

        {"pattern": "hsn_generation_request", "data": {
            "batchId": "...",
            "bids": [{"bidId": 1, "bidNumber": "GEM/...", "items": "..."}]
        }}

    Also accepts plain format::

        {"bids": [{"bid_id": "...", "item": "..."}]}

    Publishes the structured result to ``hsn_generation_results``
    in NestJS microservice format.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_hsn_results_queue

    try:
        raw_body = json.loads(message.body.decode())

        # ── Unwrap NestJS message format ─────────────────────────
        body = _unwrap_nestjs_message(raw_body)

        # ── Normalise to a list of bids ──────────────────────────
        if "bids" in body and isinstance(body["bids"], list):
            raw_bids = body["bids"]
        elif "bid_id" in body and "item" in body:
            raw_bids = [{"bid_id": body["bid_id"], "item": body["item"]}]
        elif "bidId" in body and "items" in body:
            raw_bids = [{"bidId": body["bidId"], "items": body["items"]}]
        else:
            raise ValueError(
                "Invalid message format. Expected {bids: [...]} or "
                "{bid_id, item} or {bidId, items}"
            )

        # Map NestJS fields → Python fields
        bids, id_lookup = _map_nestjs_bids_to_python(raw_bids)

        _log.info(
            "📥 HSN job received – %d bid(s): %s",
            len(bids),
            ", ".join(b.get("bid_id", "?") for b in bids[:5]),
        )

        # ── Run HSN generation ───────────────────────────────────
        result = await generate_hsn_codes(bids)

        # Map Python fields back → NestJS fields
        result = _map_results_to_nestjs(result, id_lookup)

        _log.info(
            "HSN generation done – status=%s  bids=%d",
            result.get("status"),
            result.get("meta_data", {}).get("total_bids_processed", 0),
        )

        # ── Publish result in NestJS microservice format ─────────
        nestjs_message = {
            "pattern": "hsn_generation_result",
            "data": result,
        }
        result_body = json.dumps(
            nestjs_message, ensure_ascii=False, default=str
        ).encode()

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
                "pattern": "hsn_generation_result",
                "data": {
                    "status": "failed",
                    "error": traceback.format_exc(),
                    "data": {},
                    "error_code": "worker_error",
                    "error_messages": [traceback.format_exc()],
                },
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
