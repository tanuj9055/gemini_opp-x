"""
Test script – publish a sample bid-evaluation job to RabbitMQ.

Usage::

    python test_publish_job.py

This sends a sample message to the ``bid_evaluation_jobs`` queue so the
AI worker can pick it up and process it.

Prerequisites:
  - RabbitMQ running locally (``docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:management``)
  - Worker running (``python -m app.worker.main``  or FastAPI with lifespan)
  - Valid AWS credentials in .env (for S3 downloads)
  - Valid GOOGLE_API_KEY in .env (for Gemini calls)
"""

from __future__ import annotations

import json
import sys

import pika

RABBITMQ_URL = "amqp://localhost"
JOBS_QUEUE = "bid_evaluation_jobs"
RESULTS_QUEUE = "bid_evaluation_results"


# ── Sample job payload ───────────────────────────────────────────
SAMPLE_JOB = {
    "job_id": "eval_983472",
    "bid_id": "GEM/2025/B/6716709",
    "bid_document_url": "s3://bids/bid_6716709.pdf",
    "vendors": [
        {
            "vendor_id": "vendor_01",
            "documents": [
                "s3://vendors/vendor_01/gst.pdf",
                "s3://vendors/vendor_01/pan.pdf",
                "s3://vendors/vendor_01/balance_sheet.pdf",
            ],
        },
        {
            "vendor_id": "vendor_02",
            "documents": [
                "s3://vendors/vendor_02/gst.pdf",
                "s3://vendors/vendor_02/pan.pdf",
            ],
        },
    ],
}


def publish_job(job: dict | None = None) -> None:
    """Publish a job message to the ``bid_evaluation_jobs`` queue."""
    payload = job or SAMPLE_JOB

    # Connect to RabbitMQ
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()

    # Declare queues (idempotent)
    channel.queue_declare(queue=JOBS_QUEUE, durable=True)
    channel.queue_declare(queue=RESULTS_QUEUE, durable=True)

    # Publish
    body = json.dumps(payload, ensure_ascii=False)
    channel.basic_publish(
        exchange="",
        routing_key=JOBS_QUEUE,
        body=body.encode(),
        properties=pika.BasicProperties(
            delivery_mode=2,  # persistent
            content_type="application/json",
        ),
    )

    print(f"✅ Published job to '{JOBS_QUEUE}':")
    print(json.dumps(payload, indent=2))

    connection.close()


def consume_result(timeout: int = 120) -> None:
    """Wait for and print the result message from ``bid_evaluation_results``.

    Blocks for up to *timeout* seconds.
    """
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()
    channel.queue_declare(queue=RESULTS_QUEUE, durable=True)

    print(f"\n⏳ Waiting for result on '{RESULTS_QUEUE}' (timeout={timeout}s) …\n")

    for method, properties, body in channel.consume(
        queue=RESULTS_QUEUE, inactivity_timeout=timeout,
    ):
        if method is None:
            print("⏰ Timed out waiting for result.")
            break

        result = json.loads(body.decode())
        print("📥 Result received:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        channel.basic_ack(delivery_tag=method.delivery_tag)
        break

    channel.cancel()
    connection.close()


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--consume" in sys.argv:
        # Only consume result (worker already running)
        consume_result()
    elif "--publish-only" in sys.argv:
        # Only publish, don't wait for result
        publish_job()
    else:
        # Default: publish job then wait for result
        publish_job()
        consume_result()
