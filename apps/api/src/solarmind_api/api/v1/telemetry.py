"""Ingestion (gateways) and telemetry queries (users)."""

import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import AwareDatetime
from redis.exceptions import RedisError

from solarmind_api.deps import AppSettings, Auth, AuthContext, Perm, TenantDB, get_owned
from solarmind_api.errors import RateLimited
from solarmind_api.models import Device, Site
from solarmind_api.permissions import P
from solarmind_api.services import ingestion, throttle
from solarmind_api.services import telemetry as svc
from solarmind_schemas import METRICS, TelemetryBatch

ingest_router = APIRouter(prefix="/ingestion", tags=["ingestion"])
router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@ingest_router.post("/batch")
async def ingest_batch(
    batch: TelemetryBatch,
    request: Request,
    settings: AppSettings,
    x_gateway_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Batch ingestion for edge gateways (authenticated by ``X-Gateway-Key``).

    Idempotent: replaying a batch stores nothing new and reports duplicates.
    """
    # Per-key rate limit (the key is hashed: it must not end up in Redis).
    digest = hashlib.sha256((x_gateway_key or "").encode()).hexdigest()[:24]
    key = f"rl:ingest:{digest}:{int(time.time()) // 60}"
    try:
        count = await throttle.hit(request.app.state.redis, key, 120)
    except RedisError:
        count = 0  # never drop telemetry because the limiter is down
    if count > settings.rate_limit_ingest_per_minute:
        raise RateLimited("Too many ingestion requests")

    gateway = await ingestion.authenticate_gateway(request.app.state.db, x_gateway_key)
    result = await ingestion.ingest_for_gateway(request.app.state.db, settings, gateway, batch)
    return result.summary()


@ingest_router.get("/metrics")
async def metric_registry(_: Auth) -> list[dict[str, Any]]:
    """Canonical metrics accepted by the platform, with units and valid ranges."""
    return [
        {
            "name": m.name,
            "unit": m.unit,
            "minimum": m.minimum,
            "maximum": m.maximum,
            "description": m.description,
        }  # fmt: skip
        for m in METRICS.values()
    ]


@router.get("/latest")
async def latest(
    s: TenantDB,
    auth: AuthContext = Perm(P.TELEMETRY_READ),
    site_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    max_age_hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=max_age_hours)
    return await svc.latest(s, auth.org, site_id=site_id, device_id=device_id, since=since)


@router.get("/series")
async def series(
    s: TenantDB,
    site_id: uuid.UUID,
    metric: str,
    start: AwareDatetime,
    end: AwareDatetime,
    auth: AuthContext = Perm(P.TELEMETRY_READ),
    device_id: uuid.UUID | None = None,
    bucket_seconds: Annotated[int | None, Query(ge=60, le=86400)] = None,
) -> dict[str, Any]:
    await get_owned(s, Site, site_id, auth.org)
    return await svc.series(
        s, auth.org, site_id=site_id, metric=metric, start=start, end=end,
        bucket_s=bucket_seconds, device_id=device_id,
    )  # fmt: skip


@router.get("/raw")
async def raw(
    s: TenantDB,
    device_id: uuid.UUID,
    start: AwareDatetime,
    end: AwareDatetime,
    auth: AuthContext = Perm(P.TELEMETRY_READ),
    metric: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> list[dict[str, Any]]:
    await get_owned(s, Device, device_id, auth.org)
    return await svc.raw(
        s, auth.org, device_id=device_id, metric=metric, start=start, end=end, limit=limit
    )
