"""Application settings.

Every value comes from the environment (prefix ``SOLARMIND_``). Secrets have no
default on purpose: a missing variable must fail at startup, not silently fall
back to a credential baked into the source.
"""

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_KEY_LENGTH = 32


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

    # --- database pool ---
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle_s: int = 1800
    # Upper bound for one SQL statement of the API: a runaway analytics query
    # is cancelled instead of holding a connection and a CPU indefinitely.
    db_statement_timeout_ms: int = 15_000

    # --- HTTP edge ---
    # Reverse proxies whose X-Forwarded-For header is trusted (CIDR). Empty:
    # the TCP peer is the client. Behind a proxy this MUST be set, otherwise
    # every user shares the proxy's address and its rate-limit budget.
    trusted_proxies: list[IPv4Network | IPv6Network] = []
    max_request_body_bytes: int = 1024 * 1024
    max_ingest_body_bytes: int = 8 * 1024 * 1024

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
    # Per user: each question may call a paid LLM API several times.
    rate_limit_ai_per_minute: int = 10
    # Per account, over a sliding 15-minute window, whatever the client IP:
    # defeats brute force spread over many addresses.
    login_max_failures: int = 10
    mfa_max_failures: int = 5
    account_lock_window_s: int = 900

    # --- read cache (Redis). 0 disables a cache. ---
    cache_ttl_overview_s: int = 30
    cache_ttl_insights_s: int = 300

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
    # Only the most recent messages of a conversation are sent back to the LLM:
    # bounded cost and latency, whatever the conversation length.
    ai_history_max_messages: int = 20

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"

    @property
    def is_deployed(self) -> bool:
        return self.environment in ("staging", "production")

    @model_validator(mode="after")
    def _deployed_settings_are_safe(self) -> Self:
        """Refuse to start staging/production with settings only fit for a laptop."""
        if not self.is_deployed:
            return self
        problems = []
        if len(self.secret_key.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            problems.append(f"SOLARMIND_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} chars")
        if not self.cookie_secure:
            problems.append("SOLARMIND_COOKIE_SECURE must be true (cookies over HTTPS only)")
        if not self.web_base_url.startswith("https://"):
            problems.append("SOLARMIND_WEB_BASE_URL must use https:// (links carry tokens)")
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
