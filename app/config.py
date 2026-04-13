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

    # ── AWS / S3 ──────────────────────────────────────
    aws_access_key: str = ""
    aws_secret_key: str = ""
    aws_region: str = ""
    aws_s3_bucket: str = ""

    # ── RabbitMQ ──────────────────────────────────────
    rabbitmq_url: str = "amqp://localhost"
    rabbitmq_jobs_queue: str = "bid_evaluation_jobs"
    rabbitmq_results_queue: str = "bid_evaluation_results"
    # ── RabbitMQ (HSN generation) ─────────────────────────
    rabbitmq_hsn_jobs_queue: str = "hsn_requests_queue"
    rabbitmq_hsn_results_queue: str = "hsn_generation_results"
    
    # ── RabbitMQ (PDF generation) ─────────────────────────
    rabbitmq_pdf_jobs_queue: str = "pdf_generation_jobs"
    rabbitmq_pdf_results_queue: str = "pdf_generation_results"

    # ── RabbitMQ (Tender rule extraction) ─────────────────
    rabbitmq_extraction_jobs_queue: str = "tender_extraction_jobs"
    rabbitmq_extraction_results_queue: str = "tender_extraction_results"
    
    # ── RabbitMQ (Tender Analysis Agent) ──────────────────
    rabbitmq_analysis_jobs_queue: str = "tender_analysis_jobs"
    rabbitmq_analysis_results_queue: str = "tender_analysis_results"

    # ── RabbitMQ (Classification Agent) ───────────────────
    rabbitmq_classification_jobs_queue: str = "rule_classification_jobs"
    rabbitmq_classification_results_queue: str = "rule_classification_results"

    # ── RabbitMQ (Evaluation Agent) ───────────────────────
    rabbitmq_evaluation_jobs_queue: str = "rule_evaluation_jobs"
    rabbitmq_evaluation_results_queue: str = "rule_evaluation_results"

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
