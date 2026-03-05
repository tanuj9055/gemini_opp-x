"""
RabbitMQ subscriber – listens on the ``hello_exchange`` (fanout) and
``analysis_exchange`` (fanout) exchanges and logs every received message.
"""

from __future__ import annotations

import asyncio
import json

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.logging_cfg import logger

_log = logger.getChild("rabbitmq_consumer")

HELLO_EXCHANGE = "hello_exchange"
ANALYSIS_EXCHANGE = "analysis_exchange"


async def _on_hello_message(message: AbstractIncomingMessage) -> None:
    async with message.process():
        try:
            body = json.loads(message.body.decode())
            _log.info("📨 [hello_exchange] Received: %s", body)
        except json.JSONDecodeError:
            _log.warning("Non-JSON message on hello_exchange: %s", message.body)


async def _on_analysis_message(message: AbstractIncomingMessage) -> None:
    async with message.process():
        try:
            body = json.loads(message.body.decode())
            msg_type = body.get("type", "unknown")

            if msg_type == "tender_apply":
                bid_number = body.get("bidNumber", "N/A")
                bid_url = body.get("bidUrl", "N/A")
                documents = body.get("companyDocuments", [])
                bid_details = body.get("bidDetails", {})
                print(f"\n{'='*50}")
                print(f"  📋 TENDER APPLICATION RECEIVED")
                print(f"  🔢 Bid Number : {bid_number}")
                print(f"  🔗 Bid URL    : {bid_url}")
                print(f"  📄 Documents  : {len(documents)} attached")
                for doc in documents:
                    print(f"     • {doc.get('documentType', 'N/A')} — {doc.get('fileUrl', 'N/A')}")
                print(f"  🕐 Timestamp  : {body.get('timestamp', 'N/A')}")
                print(f"{'='*50}\n")
                _log.info(
                    "[analysis_exchange] Tender apply — bid=%s, docs=%d",
                    bid_number,
                    len(documents),
                )
            else:
                company_id = body.get("companyId", "UNKNOWN")
                print(f"\n{'='*50}")
                print(f"  🔬 ANALYSIS REQUEST RECEIVED")
                print(f"  🏢 Company ID : {company_id}")
                print(f"  🕐 Timestamp  : {body.get('timestamp', 'N/A')}")
                print(f"{'='*50}\n")
                _log.info("[analysis_exchange] companyId=%s", company_id)

        except json.JSONDecodeError:
            _log.warning("Non-JSON message on analysis_exchange: %s", message.body)


async def start_consumer(rabbitmq_url: str) -> None:
    """
    Connects to RabbitMQ via a robust (auto-reconnecting) connection,
    declares both exchanges, binds exclusive queues, and starts consuming.
    """
    _log.info("Connecting RabbitMQ consumer to %s …", rabbitmq_url)
    connection = await aio_pika.connect_robust(rabbitmq_url)

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=10)

        # ── hello_exchange ────────────────────────────────────────────────
        hello_ex = await channel.declare_exchange(
            HELLO_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )
        hello_queue = await channel.declare_queue("", exclusive=True, auto_delete=True)
        await hello_queue.bind(hello_ex)
        await hello_queue.consume(_on_hello_message)

        # ── analysis_exchange ─────────────────────────────────────────────
        analysis_ex = await channel.declare_exchange(
            ANALYSIS_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )
        analysis_queue = await channel.declare_queue(
            "", exclusive=True, auto_delete=True
        )
        await analysis_queue.bind(analysis_ex)
        await analysis_queue.consume(_on_analysis_message)

        _log.info(
            "RabbitMQ consumer ready – listening on '%s' and '%s'",
            HELLO_EXCHANGE,
            ANALYSIS_EXCHANGE,
        )

        # Keep alive until lifespan cancels this task
        await asyncio.Future()

