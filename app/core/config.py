"""Central configuration, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MEDX_", extra="ignore")

    app_name: str = "MedExtract AI"
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"

    # --- OCR -------------------------------------------------------------
    ocr_provider: Literal["textract", "tesseract", "auto"] = "auto"
    # Pages whose mean OCR confidence is below this are flagged for review.
    ocr_min_confidence: float = 0.80

    # --- LLM -------------------------------------------------------------
    llm_provider: Literal["bedrock", "anthropic", "rules"] = "rules"
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_api_key: str | None = None
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.0
    llm_max_retries: int = 2  # self-correction attempts after validation failure
    llm_timeout_s: float = 60.0

    # --- Quality gates ---------------------------------------------------
    auto_approve_threshold: float = Field(0.90, ge=0, le=1)
    field_review_threshold: float = Field(0.75, ge=0, le=1)

    # --- AWS -------------------------------------------------------------
    aws_region: str = "us-east-1"
    s3_bucket: str | None = None
    sqs_queue_url: str | None = None
    dynamodb_table: str | None = None
    kms_key_id: str | None = None

    # --- Storage ---------------------------------------------------------
    storage_backend: Literal["memory", "dynamodb"] = "memory"

    # --- Security --------------------------------------------------------
    api_key: str | None = None  # if set, required in X-API-Key header
    redact_phi_in_logs: bool = True
    max_upload_mb: int = 25


@lru_cache
def get_settings() -> Settings:
    return Settings()
