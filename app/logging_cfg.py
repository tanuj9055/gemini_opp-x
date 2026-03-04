"""
Structured logging setup for the audit service.
"""

from __future__ import annotations

import logging
import sys

from app.config import get_settings


def setup_logging() -> logging.Logger:
    """Return the application root logger with a rich console handler."""
    settings = get_settings()

    fmt = (
        "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger("gem_audit")
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.DEBUG))
    if not root.handlers:
        root.addHandler(handler)
    return root


logger = setup_logging()
