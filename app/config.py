"""
Application configuration – loaded once from environment / .env file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Central configuration holder.  Values are read from env vars first,
    then from a ``.env`` file at the project root.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Gemini ────────────────────────────────────────
    google_api_key: str

    # ── Model ─────────────────────────────────────────
    gemini_model: str = "gemini-2.5-pro"

    # ── App ───────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "DEBUG"

    # ── RabbitMQ ──────────────────────────────────────
    rabbitmq_url: str = "amqp://localhost"

    # ── Upload limits ─────────────────────────────────
    max_file_size_mb: int = 50

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache()
def get_settings() -> Settings:
    """Singleton accessor – cached after first call."""
    return Settings()  # type: ignore[call-arg]
