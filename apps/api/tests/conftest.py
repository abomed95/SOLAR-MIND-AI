import os
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from solarmind_api.config import Settings
from solarmind_api.main import create_app

# Unit tests never connect: these point at a closed port unless the integration
# run overrides them with real services.
DATABASE_URL = os.environ.get(
    "SOLARMIND_DATABASE_URL", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"
)
REDIS_URL = os.environ.get("SOLARMIND_REDIS_URL", "redis://127.0.0.1:1/0")


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "database_url": SecretStr(DATABASE_URL),
        "redis_url": SecretStr(REDIS_URL),
        "readiness_timeout_s": 1.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
