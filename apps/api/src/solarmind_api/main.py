"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from redis.asyncio import Redis

from solarmind_api.api.v1 import (
    admin,
    ai,
    alerts,
    auth,
    energy,
    equipment,
    health,
    organizations,
    telemetry,
)
from solarmind_api.config import Settings, get_settings
from solarmind_api.db import Database
from solarmind_api.deps import rate_limit
from solarmind_api.errors import install_error_handlers
from solarmind_api.logging import configure_logging
from solarmind_api.middleware import EdgeMiddleware
from solarmind_api.security import SecretBox
from solarmind_api.services.ai import build_provider
from solarmind_api.services.mail import build_mailer

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The API serves JSON and PDF only: nothing may be loaded or executed from it.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}
# Sent only when the deployment is HTTPS (cookie_secure): browsers then refuse
# plain HTTP for a year, which defeats SSL-stripping on hostile networks.
_HSTS = "max-age=31536000; includeSubDomains"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Neither object opens a connection here; both connect lazily on first use.
    database = Database.from_settings(settings)
    redis_client = Redis.from_url(settings.redis_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await redis_client.aclose()
        await database.dispose()

    app = FastAPI(
        title="SOLAR MIND AI API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.redis = redis_client
    app.state.secret_box = SecretBox(settings.secret_key.get_secret_value())
    app.state.mailer = build_mailer(settings)
    app.state.llm_provider = build_provider(settings)

    async def _redis_ping() -> None:
        await redis_client.ping()

    app.state.probes = {"database": database.ping, "redis": _redis_ping}

    install_error_handlers(app)
    headers = dict(_SECURITY_HEADERS)
    if settings.cookie_secure:
        headers["Strict-Transport-Security"] = _HSTS
    app.add_middleware(
        EdgeMiddleware,
        security_headers=headers,
        trusted_proxies=settings.trusted_proxies,
        max_body_bytes=settings.max_request_body_bytes,
        max_ingest_body_bytes=settings.max_ingest_body_bytes,
    )

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    limited = APIRouter(dependencies=[Depends(rate_limit("api", "rate_limit_api_per_minute"))])
    for router in (
        auth.router,
        organizations.org_router,
        organizations.users_router,
        equipment.sites,
        equipment.assets,
        equipment.devices,
        equipment.gateways,
        telemetry.router,
        energy.router,
        alerts.router,
        alerts.notifications,
        ai.router,
        admin.router,
    ):
        limited.include_router(router)
    v1.include_router(limited)
    # Gateways are limited per key inside the endpoint, not per IP.
    v1.include_router(telemetry.ingest_router)
    app.include_router(v1)
    return app
