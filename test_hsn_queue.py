"""
Test script – publish HSN generation jobs and consume results.

Usage::

    python test_hsn_queue.py                # publish + wait for result
    python test_hsn_queue.py --publish-only # just publish, don't wait
    python test_hsn_queue.py --consume      # just wait for result
    python test_hsn_queue.py --batch        # publish a batch of items

Prerequisites:
  - RabbitMQ running locally
      docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:management
  - HSN worker running (FastAPI app or ``python -m app.worker.hsn_main``)
  - Valid GOOGLE_API_KEY in .env (for Gemini calls)
"""

from __future__ import annotations

import json
import sys

import pika

RABBITMQ_URL = "amqp://localhost"
HSN_JOBS_QUEUE = "hsn_generation_jobs"
HSN_RESULTS_QUEUE = "hsn_generation_results"


# ── Sample payloads ─────────────────────────────────────────────

SINGLE_ITEM = {
    "bid_id": "GEM/2024/B/12345",
    "item": "Laptop computer with 8GB RAM, 256GB SSD, Intel i5 processor, 15.6 inch display",
}

BATCH_ITEMS = {
    "bids": [
        {
            "bid_id": "GEM/2024/B/10001",
            "item": "Laptop computer with 8GB RAM, 256GB SSD, Intel i5 processor",
        },
        {
            "bid_id": "GEM/2024/B/10002",
            "item": "Office revolving chairs with armrest, mesh back, hydraulic height adjustment",
        },
        {
            "bid_id": "GEM/2024/B/10003",
            "item": "Laser printer monochrome A4 size with duplex printing and network connectivity",
        },
    ],
}


def publish_job(payload: dict) -> None:
    """Publish an HSN job to ``hsn_generation_jobs``."""
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()

    channel.queue_declare(queue=HSN_JOBS_QUEUE, durable=True)
    channel.queue_declare(queue=HSN_RESULTS_QUEUE, durable=True)

    body = json.dumps(payload, ensure_ascii=False)
    channel.basic_publish(
        exchange="",
        routing_key=HSN_JOBS_QUEUE,
        body=body.encode(),
        properties=pika.BasicProperties(
            delivery_mode=2,
            content_type="application/json",
        ),
    )

    print(f"✅ Published HSN job to '{HSN_JOBS_QUEUE}':")
    print(json.dumps(payload, indent=2))
    connection.close()


def consume_result(timeout: int = 120) -> None:
    """Wait for the HSN result on ``hsn_generation_results``."""
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()
    channel.queue_declare(queue=HSN_RESULTS_QUEUE, durable=True)

    print(f"\n⏳ Waiting for HSN result on '{HSN_RESULTS_QUEUE}' (timeout={timeout}s) …\n")

    for method, properties, body in channel.consume(
        queue=HSN_RESULTS_QUEUE, inactivity_timeout=timeout,
    ):
        if method is None:
            print("⏰ Timed out waiting for HSN result.")
            break

        result = json.loads(body.decode())
        print("📥 HSN Result received:")
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # Pretty-print the HSN codes if successful
        if result.get("status") == "success":
            results = result.get("data", {}).get("results", [])
            print(f"\n🏷️  HSN Codes ({len(results)} items):")
            for r in results:
                print(
                    f"   {r.get('bid_id', '?'):30s} → HSN {r.get('hsn', '?'):10s} "
                    f"[{r.get('confidence', '?')}]  {r.get('reasoning', '')[:60]}"
                )
        else:
            print(f"\n❌ Error: {result.get('error_code', '?')}")
            for msg in result.get("error_messages", []):
                print(f"   {msg[:120]}")

        channel.basic_ack(delivery_tag=method.delivery_tag)
        break

    channel.cancel()
    connection.close()


if __name__ == "__main__":
    if "--consume" in sys.argv:
        consume_result()
    elif "--publish-only" in sys.argv:
        payload = BATCH_ITEMS if "--batch" in sys.argv else SINGLE_ITEM
        publish_job(payload)
    elif "--batch" in sys.argv:
        publish_job(BATCH_ITEMS)
        consume_result()
    else:
        publish_job(SINGLE_ITEM)
        consume_result()
