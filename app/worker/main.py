"""
Standalone entry point for the AI Worker.

Usage::

    python -m app.worker.main

This starts the RabbitMQ consumer directly (no FastAPI / HTTP server).
"""

from app.worker.consumer import run_worker

if __name__ == "__main__":
    run_worker()
