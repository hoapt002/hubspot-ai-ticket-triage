"""Runtime configuration, read from environment variables (or a .env file)."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://localhost:5432/ticket_triage"
    session_secret: SecretStr = SecretStr("")
    public_base_url: str = "http://localhost:8000"
    secure_cookies: bool = False

    taxonomy_path: Path = PROJECT_ROOT / "config" / "taxonomy.yaml"

    # Webhook sources: {"source-name": "shared-secret"}. Each source signs its
    # requests with HMAC-SHA256 (see webhooks/signature.py).
    webhook_secrets: dict[str, SecretStr] = Field(default_factory=dict)
    webhook_tolerance_seconds: int = 300
    # Postmark inbound email webhook, protected with HTTP basic auth.
    postmark_inbound_user: str = ""
    postmark_inbound_password: SecretStr = SecretStr("")

    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_model: str = ""
    llm_timeout_seconds: float = 60.0
    # USD per million tokens, used for cost-per-ticket reporting.
    llm_price_input_per_mtok: Decimal = Decimal("0")
    llm_price_output_per_mtok: Decimal = Decimal("0")
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    hubspot_access_token: SecretStr = SecretStr("")
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_pipeline_id: str = "0"
    hubspot_pipeline_stage_id: str = "1"
    hubspot_portal_id: str = ""

    # Share of tickets (0..1) routed to fully manual triage. The AI still runs
    # on them in shadow mode, which gives an unbiased accuracy measurement and a
    # real manual handling-time baseline.
    manual_baseline_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    worker_poll_interval_seconds: float = 1.0
    worker_stale_lock_seconds: int = 600
    job_max_attempts: int = 5

    @field_validator("llm_model")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @property
    def resolved_llm_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return "claude-opus-5-5" if self.llm_provider == "anthropic" else "gpt-5.4-mini"

    @property
    def hubspot_enabled(self) -> bool:
        return bool(self.hubspot_access_token.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
