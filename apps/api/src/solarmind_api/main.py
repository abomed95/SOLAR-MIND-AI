"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from solarmind_api.api.v1 import health
from solarmind_api.config import Settings, get_settings
from solarmind_api.logging import configure_logging


def _database_probe(engine: AsyncEngine) -> health.Probe:
    async def probe() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return probe


def _redis_probe(client: Redis) -> health.Probe:
    async def probe() -> None:
        await client.ping()

    return probe


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Neither object opens a connection here; both connect lazily on first use.
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    redis_client = Redis.from_url(settings.redis_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await redis_client.aclose()
        await engine.dispose()

    app = FastAPI(
        title="SOLAR MIND AI API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.redis = redis_client
    app.state.probes = {
        "database": _database_probe(engine),
        "redis": _redis_probe(redis_client),
    }

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    app.include_router(v1)
    return app
