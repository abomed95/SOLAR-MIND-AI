"""Application settings.

Every value comes from the environment (prefix ``SOLARMIND_``). Secrets have no
default on purpose: a missing variable must fail at startup, not silently fall
back to a credential baked into the source.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLARMIND_", extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Application role, subject to Row-Level Security (never the table owner).
    # e.g. postgresql+asyncpg://solarmind_app:password@host:5432/dbname
    database_url: SecretStr
    redis_url: SecretStr
    # Used to encrypt MFA secrets at rest. Rotating it invalidates enrolled MFA.
    secret_key: SecretStr

    readiness_timeout_s: float = 2.0

    # --- identity ---
    session_ttl_hours: int = 12
    invitation_ttl_hours: int = 72
    password_reset_ttl_minutes: int = 30
    cookie_secure: bool = True
    web_base_url: str = "http://localhost:3000"
    trial_days: int = 30

    # --- rate limits (requests per minute, per client) ---
    rate_limit_auth_per_minute: int = 10
    rate_limit_api_per_minute: int = 600
    rate_limit_ingest_per_minute: int = 600

    # --- telemetry ---
    max_future_skew_s: int = 300
    max_batch_records: int = 5000
    gateway_offline_after_s: int = 900

    # --- object storage ---
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = "/var/lib/solarmind/storage"
    s3_endpoint: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_bucket: str = "solarmind"
    s3_secure: bool = True
    max_upload_bytes: int = 10 * 1024 * 1024

    # --- MQTT (worker only) ---
    mqtt_host: str | None = None
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None

    # --- outbound notifications ---
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "no-reply@solarmind.local"
    notification_max_attempts: int = 5

    # --- LLM (assistant). "deterministic" needs no network and no key. ---
    llm_provider: Literal["deterministic", "openai"] = "deterministic"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
