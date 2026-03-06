"""
Test script – publish a tender_apply event to analysis_exchange.

Simulates the NestJS backend sending a tender application with hardcoded
documents (bid PDF on S3).  The FastAPI consumer picks it up, runs the
Gemini pipeline, and publishes the result to analysis_results_exchange.

Usage::

    python test_tender_apply.py

Prerequisites:
  - RabbitMQ running locally
  - FastAPI server running (uvicorn app.main:app --reload)
  - Valid .env with GOOGLE_API_KEY and AWS credentials
"""

from __future__ import annotations

import json
import sys
import time

import pika

RABBITMQ_URL = "amqp://localhost"
ANALYSIS_EXCHANGE = "analysis_exchange"
RESULTS_EXCHANGE = "analysis_results_exchange"

# ── Hardcoded tender_apply payload ────────────────────────────────
TEST_MESSAGE = {
    "type": "tender_apply",
    "bidNumber": "GEM/2025/B/6756124",
    "bidUrl": "https://bidplus.gem.gov.in/showbidDocument/8434342",
    "companyDocuments": [
        {
            "documentType": "Reference Letters",
            "fileUrl": "https://tender-demo-storage-123.s3.ap-south-1.amazonaws.com/qistonpe/documents/28507005-5cb0-4573-96e6-f4f81d18e046/1772709113392-GeM-Bidding-8481457.pdf",
        }
    ],
    "bidDetails": {},
    "timestamp": "2026-03-06T07:28:59.815Z",
}


def main() -> None:
    print("Connecting to RabbitMQ …")
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()

    # Declare exchanges (idempotent – must match what the consumer declared)
    channel.exchange_declare(exchange=ANALYSIS_EXCHANGE, exchange_type="fanout", durable=True)
    channel.exchange_declare(exchange=RESULTS_EXCHANGE, exchange_type="fanout", durable=True)

    # ── Publish ──────────────────────────────────────────────────
    body = json.dumps(TEST_MESSAGE, ensure_ascii=False).encode()
    channel.basic_publish(
        exchange=ANALYSIS_EXCHANGE,
        routing_key="",
        body=body,
        properties=pika.BasicProperties(
            delivery_mode=2,
            content_type="application/json",
        ),
    )
    print(f"📤 Published test tender_apply to '{ANALYSIS_EXCHANGE}'")
    print(json.dumps(TEST_MESSAGE, indent=2))

    # ── Wait for result on analysis_results_exchange ─────────────
    print("\n⏳ Waiting for pipeline result (timeout: 5 min) …")

    # Bind a temporary queue to the results exchange
    result = channel.queue_declare(queue="", exclusive=True)
    tmp_queue = result.method.queue
    channel.queue_bind(exchange=RESULTS_EXCHANGE, queue=tmp_queue)

    received = False
    start = time.time()
    timeout = 300  # 5 minutes

    def on_result(ch, method, properties, body_bytes):
        nonlocal received
        received = True
        elapsed = time.time() - start
        data = json.loads(body_bytes.decode())
        print(f"\n✅ RESULT RECEIVED in {elapsed:.1f}s")
        print(f"   Status : {data.get('status')}")
        print(f"   Bid    : {data.get('bidNumber')}")
        if data.get("status") == "completed":
            ba = data.get("bid_analysis", {})
            print(f"   Criteria: {len(ba.get('eligibility_criteria', []))}")
            vr = data.get("vendor_results", [])
            print(f"   Vendors : {len(vr)}")
            for v in vr:
                print(f"     - {v.get('vendor_id')}: score={v.get('eligibility_score')}  rec={v.get('recommendation')}")
        else:
            print(f"   Error  : {data.get('error', 'N/A')}")

        # Save full result to file
        with open("_test_result.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        print("\n   Full result saved to _test_result.json")
        ch.stop_consuming()

    channel.basic_consume(queue=tmp_queue, on_message_callback=on_result, auto_ack=True)

    # Consume with timeout
    try:
        while not received and (time.time() - start) < timeout:
            connection.process_data_events(time_limit=1)
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")

    if not received:
        print(f"\n❌ TIMEOUT – no result received within {timeout}s")

    connection.close()


if __name__ == "__main__":
    main()
