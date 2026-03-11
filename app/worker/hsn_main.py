"""
Standalone entry point for the HSN Worker.

Usage::

    python -m app.worker.hsn_main

This starts the HSN RabbitMQ consumer directly (no FastAPI / HTTP server).
"""

from app.worker.hsn_consumer import run_hsn_worker

if __name__ == "__main__":
    run_hsn_worker()
