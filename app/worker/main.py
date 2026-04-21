"""
Standalone entry point for the AI Worker.

Usage::

    python -m app.worker.main

This starts the RabbitMQ consumer directly (no FastAPI / HTTP server).
"""

import asyncio
from app.worker.consumer import start_worker
from app.worker.pdf_consumer import start_pdf_worker
from app.worker.filter_consumer import start_filter_worker
from app.logging_cfg import logger

_log = logger.getChild("worker_main")

async def main():
    _log.info("🚀 Starting all AI Workers (standalone mode)")
    await asyncio.gather(
        start_worker(),
        start_pdf_worker(),
        start_filter_worker()
    )

def run_all_workers():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log.info("👋 Workers stopped by user")

if __name__ == "__main__":
    run_all_workers()
