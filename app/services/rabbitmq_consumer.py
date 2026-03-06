"""
RabbitMQ subscriber – listens on ``analysis_exchange`` (fanout) for
tender-apply requests from the NestJS backend, runs the full Gemini
AI pipeline, and publishes results to ``analysis_results_exchange``.

Also keeps the ``hello_exchange`` listener for heartbeat / test messages.

Message contract (NestJS → Python)
----------------------------------
Published to ``analysis_exchange`` (fanout)::

    {
      "type": "tender_apply",
      "bidNumber": "8481457",
      "bidUrl": "s3://bucket/path/GeM-Bidding-8481457.pdf",
      "companyDocuments": [
        {"documentType": "gst", "fileUrl": "s3://bucket/vendor/gst.pdf"},
        {"documentType": "pan", "fileUrl": "s3://bucket/vendor/pan.pdf"}
      ],
      "bidDetails": { ... optional bid metadata ... },
      "timestamp": "2026-03-06T12:00:00Z"
    }

Result contract (Python → NestJS)
---------------------------------
Published to ``analysis_results_exchange`` (fanout)::

    {
      "type": "tender_result",
      "bidNumber": "8481457",
      "status": "completed" | "failed",
      "bid_analysis": { ... full Stage 1 response ... },
      "vendor_results": [ ... full Stage 2 response ... ],
      "errors": [],
      "processing_time_seconds": 147.02
    }
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.logging_cfg import logger
from app.worker.job_processor import process_evaluation_job

_log = logger.getChild("rabbitmq_consumer")

HELLO_EXCHANGE = "hello_exchange"
ANALYSIS_EXCHANGE = "analysis_exchange"
ANALYSIS_RESULTS_EXCHANGE = "analysis_results_exchange"


# ────────────────────────────────────────────────────────
# hello_exchange handler (heartbeat / test)
# ────────────────────────────────────────────────────────

async def _on_hello_message(message: AbstractIncomingMessage) -> None:
    async with message.process():
        try:
            body = json.loads(message.body.decode())
            _log.info("📨 [hello_exchange] Received: %s", body)
        except json.JSONDecodeError:
            _log.warning("Non-JSON message on hello_exchange: %s", message.body)


# ────────────────────────────────────────────────────────
# analysis_exchange handler – tender_apply pipeline
# ────────────────────────────────────────────────────────

async def _on_analysis_message(
    message: AbstractIncomingMessage,
    results_exchange: aio_pika.abc.AbstractExchange,
) -> None:
    """Handle a ``tender_apply`` message from the NestJS backend.

    1. Parse the NestJS payload (bidNumber, bidUrl, companyDocuments).
    2. Map to the internal job format expected by ``process_evaluation_job``.
    3. Run the full Gemini pipeline.
    4. Publish the result to ``analysis_results_exchange``.
    """
    async with message.process():
        bid_number = "UNKNOWN"
        t0 = time.perf_counter()
        try:
            body = json.loads(message.body.decode())
            msg_type = body.get("type", "unknown")

            # ── Handle tender_apply (primary flow) ───────────────
            if msg_type == "tender_apply":
                bid_number = body.get("bidNumber", "UNKNOWN")
                bid_url = body.get("bidUrl", "")
                company_docs = body.get("companyDocuments", [])
                bid_details = body.get("bidDetails", {})

                print(f"\n{'='*60}")
                print(f"  📋 TENDER APPLICATION RECEIVED")
                print(f"  🔢 Bid Number  : {bid_number}")
                print(f"  🔗 Bid URL     : {bid_url}")
                print(f"  📄 Documents   : {len(company_docs)} attached")
                for doc in company_docs:
                    print(f"     • {doc.get('documentType', 'N/A')} — {doc.get('fileUrl', 'N/A')}")
                print(f"  🕐 Timestamp   : {body.get('timestamp', 'N/A')}")
                print(f"{'='*60}\n")

                _log.info(
                    "[analysis_exchange] tender_apply — bid=%s  bidUrl=%s  docs=%d",
                    bid_number, bid_url, len(company_docs),
                )

                # ── Validate required fields ─────────────────────
                if not company_docs:
                    raise ValueError("Missing or empty 'companyDocuments' in tender_apply message")

                # ── Separate bid PDF from vendor documents ───────
                # The bidUrl from NestJS points to the GeM portal page
                # (not a direct download).  The actual bid PDF is
                # usually included in companyDocuments with a filename
                # like "GeM-Bidding-*.pdf".  We identify it here.
                bid_doc_url = None
                vendor_doc_urls: list[str] = []

                for doc in company_docs:
                    url = doc.get("fileUrl", "")
                    if not url:
                        continue
                    # Heuristic: filename contains "GeM-Bidding" or "gem-bidding"
                    lower_url = url.lower()
                    if "gem-bidding" in lower_url or "gem_bidding" in lower_url:
                        bid_doc_url = url
                    else:
                        vendor_doc_urls.append(url)

                # Fallback: if no doc matched the bid pattern, use the
                # first document as the bid PDF.
                if not bid_doc_url:
                    all_urls = [d["fileUrl"] for d in company_docs if d.get("fileUrl")]
                    if all_urls:
                        bid_doc_url = all_urls[0]
                        vendor_doc_urls = all_urls[1:]

                if not bid_doc_url:
                    raise ValueError(
                        "Could not determine the bid document URL. "
                        "Provide a direct S3 URL in companyDocuments or bidUrl."
                    )

                _log.info(
                    "[%s] Bid PDF resolved from companyDocuments: %s",
                    bid_number, bid_doc_url,
                )

                job = {
                    "job_id": f"tender_{bid_number}_{int(time.time())}",
                    "bid_id": bid_number,
                    "bid_document_url": bid_doc_url,
                    "bid_portal_url": bid_url,        # keep portal link as metadata
                    "vendors": [
                        {
                            "vendor_id": "applicant",
                            "documents": vendor_doc_urls,
                        }
                    ] if vendor_doc_urls else [],
                }

            else:
                # ── Fallback for other message types ─────────────
                bid_number = body.get("companyId", body.get("bidNumber", "UNKNOWN"))
                bid_url = body.get("bid_document_url", body.get("bidUrl", ""))
                vendors = body.get("vendors", [])

                print(f"\n{'='*60}")
                print(f"  🔬 ANALYSIS REQUEST RECEIVED (type={msg_type})")
                print(f"  🏢 ID          : {bid_number}")
                print(f"  📄 Bid Document: {bid_url}")
                print(f"  👥 Vendors     : {len(vendors)}")
                print(f"  🕐 Timestamp   : {body.get('timestamp', 'N/A')}")
                print(f"{'='*60}\n")

                _log.info(
                    "[analysis_exchange] type=%s — id=%s  bid=%s  vendors=%d",
                    msg_type, bid_number, bid_url, len(vendors),
                )

                if not bid_url:
                    raise ValueError("Missing bid document URL")
                if not vendors:
                    raise ValueError("Missing or empty vendors list")

                job = {
                    "job_id": f"generic_{bid_number}_{int(time.time())}",
                    "bid_id": bid_number,
                    "bid_document_url": bid_url,
                    "vendors": vendors,
                }

            _log.info("[%s] Starting Gemini pipeline …", job["job_id"])

            # ── Run the full pipeline ────────────────────────────
            result = await process_evaluation_job(job)

            # ── Attach identifiers to result ─────────────────────
            result["type"] = "tender_result"
            result["bidNumber"] = bid_number

            elapsed = time.perf_counter() - t0
            result["processing_time_seconds"] = round(elapsed, 2)

            _log.info(
                "[%s] Pipeline complete – status=%s  elapsed=%.1fs",
                job["job_id"], result.get("status"), elapsed,
            )

        except json.JSONDecodeError:
            _log.warning("Non-JSON message on analysis_exchange: %s", message.body[:200])
            result = {
                "type": "tender_result",
                "bidNumber": bid_number,
                "status": "failed",
                "error": "Non-JSON message received",
                "bid_analysis": None,
                "vendor_results": [],
                "errors": ["Non-JSON message received"],
            }

        except Exception as exc:
            _log.exception("[%s] Pipeline failed", bid_number)
            result = {
                "type": "tender_result",
                "bidNumber": bid_number,
                "status": "failed",
                "error": str(exc),
                "bid_analysis": None,
                "vendor_results": [],
                "errors": [traceback.format_exc()],
            }

        # ── Publish result to analysis_results_exchange ──────────
        try:
            result_body = json.dumps(result, ensure_ascii=False, default=str).encode()
            await results_exchange.publish(
                aio_pika.Message(
                    body=result_body,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key="",  # fanout — routing key ignored
            )
            _log.info(
                "📤 Result published to '%s' for bidNumber=%s – status=%s",
                ANALYSIS_RESULTS_EXCHANGE, bid_number, result.get("status"),
            )
        except Exception:
            _log.exception("Failed to publish result for bidNumber=%s", bid_number)


# ────────────────────────────────────────────────────────
# Consumer entry point
# ────────────────────────────────────────────────────────

async def start_consumer(rabbitmq_url: str) -> None:
    """
    Connects to RabbitMQ via a robust (auto-reconnecting) connection,
    declares exchanges, binds exclusive queues, and starts consuming.

    Exchanges:
      - ``hello_exchange``             (fanout, consume)  — heartbeat
      - ``analysis_exchange``          (fanout, consume)  — NestJS requests
      - ``analysis_results_exchange``  (fanout, publish)  — results back to NestJS
    """
    _log.info("Connecting RabbitMQ consumer to %s …", rabbitmq_url)
    connection = await aio_pika.connect_robust(rabbitmq_url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)

        # ── hello_exchange (heartbeat) ────────────────────────────
        hello_ex = await channel.declare_exchange(
            HELLO_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )
        hello_queue = await channel.declare_queue("", exclusive=True, auto_delete=True)
        await hello_queue.bind(hello_ex)
        await hello_queue.consume(_on_hello_message)

        # ── analysis_exchange (incoming requests from NestJS) ─────
        analysis_ex = await channel.declare_exchange(
            ANALYSIS_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )
        analysis_queue = await channel.declare_queue(
            "", exclusive=True, auto_delete=True
        )
        await analysis_queue.bind(analysis_ex)

        # ── analysis_results_exchange (outgoing results to NestJS) ─
        results_ex = await channel.declare_exchange(
            ANALYSIS_RESULTS_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )

        # Bind the analysis handler with access to the results exchange
        await analysis_queue.consume(
            lambda msg: _on_analysis_message(msg, results_ex)
        )

        _log.info(
            "RabbitMQ consumer ready – listening on '%s' and '%s', publishing to '%s'",
            HELLO_EXCHANGE,
            ANALYSIS_EXCHANGE,
            ANALYSIS_RESULTS_EXCHANGE,
        )

        # Keep alive until lifespan cancels this task
        await asyncio.Future()

