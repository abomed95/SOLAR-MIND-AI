"""Shared fixtures.

Tests marked ``db`` run against a real PostgreSQL/TimescaleDB and Redis (the
``postgres`` and ``redis`` services of docker-compose.yml). Tenant isolation is
only meaningful against the real engine, so nothing here is mocked.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
import redis as sync_redis
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

PG_HOST = os.environ.get("TEST_PG_HOST", "127.0.0.1")
PG_PORT = os.environ.get("TEST_PG_PORT", os.environ.get("POSTGRES_PORT", "5432"))
PG_OWNER = os.environ.get("POSTGRES_USER", "solarmind")
PG_OWNER_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
APP_PASSWORD = os.environ.get("SOLARMIND_APP_DB_PASSWORD", "")
TEST_DB = "solarmind_test"
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

OWNER_DSN = f"postgresql://{PG_OWNER}:{PG_OWNER_PASSWORD}@{PG_HOST}:{PG_PORT}"
APP_URL = f"postgresql+asyncpg://solarmind_app:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB}"


class CapturingMailer:
    """Test double for the e-mail provider: keeps messages instead of sending."""

    name = "capture"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))

    def last_token(self) -> str:
        return self.sent[-1][2].split("token=")[1].split()[0]


@pytest.fixture(scope="session")
def _database() -> Iterator[str]:
    """Create a fresh test database and migrate it to head, once per run."""
    with psycopg.connect(f"{OWNER_DSN}/postgres", autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")
    os.environ["SOLARMIND_MIGRATION_DATABASE_URL"] = (
        f"postgresql+psycopg://{PG_OWNER}:{PG_OWNER_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB}"
    )
    command.upgrade(Config(str(ROOT / "database" / "alembic.ini")), "head")
    yield f"{OWNER_DSN}/{TEST_DB}"


@pytest.fixture
def owner_dsn(_database: str) -> str:
    return _database


@pytest.fixture
def clean(_database: str) -> None:
    """Empty every table and the rate-limit counters before a test."""
    from solarmind_api.models import Base

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with psycopg.connect(_database, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {tables} CASCADE")
    client = sync_redis.Redis.from_url(REDIS_URL)
    client.flushdb()
    client.close()


def make_settings(**overrides: object):  # type: ignore[no-untyped-def]
    from solarmind_api.config import Settings

    values: dict[str, object] = {
        "environment": "test",
        "database_url": SecretStr(APP_URL),
        "redis_url": SecretStr(REDIS_URL),
        "secret_key": SecretStr("test-secret-key-not-used-anywhere-else"),
        "cookie_secure": False,
        "rate_limit_auth_per_minute": 10_000,
        "rate_limit_api_per_minute": 100_000,
        "rate_limit_ingest_per_minute": 100_000,
        "rate_limit_ai_per_minute": 100_000,
        # Tests assert fresh values right after ingesting: no read cache unless
        # a test enables it explicitly (tests/integration/test_hardening.py).
        "cache_ttl_overview_s": 0,
        "cache_ttl_insights_s": 0,
        "storage_local_path": f"/tmp/solarmind-test-storage-{uuid.uuid4().hex}",  # noqa: S108
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
async def app(clean: None) -> AsyncIterator[FastAPI]:
    from solarmind_api.main import create_app

    application = create_app(make_settings())
    application.state.mailer = CapturingMailer()
    yield application
    await application.state.redis.aclose()
    await application.state.db.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
