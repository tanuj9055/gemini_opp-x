"""
RabbitMQ PDF Worker – consumes PDF generation jobs and publishes results.
"""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.config import get_settings
from app.logging_cfg import logger
from app.schemas import PDFGenerationRequest, PDFGenerationResponse
from app.services.bid_package_generator import generate_bid_package_pdf
from app.services.s3_client import download_s3_url, upload_bytes_to_s3

_log = logger.getChild("pdf_worker")


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


async def _on_pdf_message(
    message: AbstractIncomingMessage,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    """Process a single PDF generation job from RabbitMQ.
    """
    settings = get_settings()
    results_queue_name = settings.rabbitmq_pdf_results_queue

    try:
        raw_body = json.loads(message.body.decode())

        # ── Unwrap NestJS message format ─────────────────────────
        body = _unwrap_nestjs_message(raw_body)

        try:
            request = PDFGenerationRequest(**body)
        except Exception as e:
            raise ValueError(f"Invalid message format. Validation error: {e}")

        companyId = request.companyId
        customerId = request.customerId

        _log.info(
            "📥 PDF job received – Company: %s, Customer: %s, %d docs",
            companyId, customerId, len(request.docsLink)
        )

        # ── Download files ───────────────────────────────────────
        vendor_files_dict = {}
        for url in request.docsLink:
            try:
                # Guess filename from URL
                filename = url.split("?")[0].split("/")[-1]
                if not filename:
                    filename = f"file_{uuid.uuid4().hex[:8]}.pdf"
                
                _log.debug("Downloading %s ...", url)
                file_bytes = await download_s3_url(url)
                vendor_files_dict[filename] = file_bytes
            except Exception as e:
                _log.error("Failed to download %s: %s", url, e)
                # Continue if some fail or should we fail the whole job? We'll continue.

        # ── Run PDF generation ───────────────────────────────────
        pdf_bytes = await generate_bid_package_pdf(
            request.bid_analysis, 
            request.vendor_evaluation, 
            vendor_files_dict
        )

        # ── Upload to S3 ─────────────────────────────────────────
        if not settings.aws_s3_bucket:
            raise EnvironmentError("aws_s3_bucket is not set in environment or config.")

        s3_key = f"generated_pdfs/{companyId}/{uuid.uuid4()}.pdf"
        s3_url = await upload_bytes_to_s3(pdf_bytes, bucket=settings.aws_s3_bucket, key=s3_key)

        _log.info("PDF generation done – uploaded to %s", s3_url)

        # ── Construct result and publish ─────────────────────────
        result = PDFGenerationResponse(
            companyId=companyId,
            customerId=customerId,
            status="success",
            pdf_url=s3_url
        )

        nestjs_message = {
            "pattern": "pdf_generation_result",
            "data": result.model_dump(),
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
            "📤 PDF result published to '%s' – status=success",
            results_queue_name
        )

        await message.ack()

    except json.JSONDecodeError:
        _log.error("❌ Non-JSON message on PDF queue – rejecting: %s", message.body[:200])
        await message.reject(requeue=False)

    except Exception as e:
        _log.error("❌ PDF job failed:\n%s", traceback.format_exc())

        # Publish an error so the caller knows
        try:
            # We attempt to extract companyId and customerId if available
            raw_body = json.loads(message.body.decode()) if hasattr(message, "body") else {}
            body = _unwrap_nestjs_message(raw_body) if isinstance(raw_body, dict) else {}
            
            cId = body.get("companyId", "unknown") if isinstance(body, dict) else "unknown"
            custId = body.get("customerId", "unknown") if isinstance(body, dict) else "unknown"

            error_result = PDFGenerationResponse(
                companyId=cId,
                customerId=custId,
                status="failed",
                error=str(e),
            )
            nestjs_message = {
                "pattern": "pdf_generation_result",
                "data": error_result.model_dump(),
            }
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(nestjs_message, default=str).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=results_queue_name,
            )
        except Exception:
            _log.error("Failed to publish PDF error result")

        await message.reject(requeue=False)


# ────────────────────────────────────────────────────────
# Consumer entry point
# ────────────────────────────────────────────────────────

async def start_pdf_worker(rabbitmq_url: str | None = None) -> None:
    """Connect to RabbitMQ, declare PDF queues, and start consuming.
    """
    settings = get_settings()
    url = rabbitmq_url or settings.rabbitmq_url
    jobs_queue_name = settings.rabbitmq_pdf_jobs_queue
    results_queue_name = settings.rabbitmq_pdf_results_queue

    _log.info("🔌 Connecting PDF worker to RabbitMQ at %s …", url)

    connection = await aio_pika.connect_robust(url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)

        # Declare both queues (idempotent)
        jobs_queue = await channel.declare_queue(jobs_queue_name, durable=True)
        await channel.declare_queue(results_queue_name, durable=True)

        _log.info(
            "✅ PDF worker ready – consuming from '%s', publishing to '%s'",
            jobs_queue_name,
            results_queue_name,
        )

        async with jobs_queue.iterator() as queue_iter:
            async for message in queue_iter:
                await _on_pdf_message(message, channel)


def run_pdf_worker() -> None:
    """Synchronous entry point for standalone mode."""
    _log.info("🚀 Starting PDF Worker (standalone mode)")
    try:
        asyncio.run(start_pdf_worker())
    except KeyboardInterrupt:
        _log.info("👋 PDF Worker stopped by user")


if __name__ == "__main__":
    run_pdf_worker()
