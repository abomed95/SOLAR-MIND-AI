"""Application settings.

Every value comes from the environment (prefix ``SOLARMIND_``). Connection
strings have no default on purpose: a missing variable must fail at startup,
not silently fall back to a credential baked into the source.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLARMIND_", extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # e.g. postgresql+asyncpg://user:password@host:5432/dbname
    database_url: SecretStr
    # e.g. redis://host:6379/0
    redis_url: SecretStr

    # Upper bound for each dependency probe of the readiness endpoint.
    readiness_timeout_s: float = 2.0

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
