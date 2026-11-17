"""Energy analytics endpoints (dashboard, site KPIs, equipment health)."""

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime

from solarmind_api.deps import AuthContext, Perm, TenantDB, get_owned
from solarmind_api.models import Site
from solarmind_api.permissions import P
from solarmind_api.services import cache, energy

router = APIRouter(prefix="/energy", tags=["energy"])


@router.get("/overview")
async def overview(
    request: Request, response: Response, s: TenantDB, auth: AuthContext = Perm(P.TELEMETRY_READ)
) -> dict[str, Any]:
    """Organization dashboard. Cached for ``cache_ttl_overview_s`` seconds:
    dashboards poll it, and it integrates a month of telemetry per site."""
    value, hit = await cache.get_or_compute(
        request.app.state.redis,
        cache.key(auth.org, "overview"),
        request.app.state.settings.cache_ttl_overview_s,
        lambda: energy.overview(s, auth.org),
    )
    response.headers["X-Cache"] = "HIT" if hit else "MISS"
    return value  # type: ignore[no-any-return]


@router.get("/equipment-health")
async def equipment_health(
    s: TenantDB, auth: AuthContext = Perm(P.TELEMETRY_READ)
) -> list[dict[str, Any]]:
    return await energy.equipment_health(s, auth.org)


@router.get("/sites/{site_id}/summary")
async def site_summary(
    site_id: uuid.UUID, start: AwareDatetime, end: AwareDatetime, s: TenantDB,
    auth: AuthContext = Perm(P.TELEMETRY_READ),
) -> dict[str, Any]:  # fmt: skip
    site = await get_owned(s, Site, site_id, auth.org)
    return await energy.site_summary(s, auth.org, site, start, end)


@router.get("/sites/{site_id}/daily")
async def site_daily(
    site_id: uuid.UUID, start: AwareDatetime, end: AwareDatetime, s: TenantDB,
    auth: AuthContext = Perm(P.TELEMETRY_READ),
) -> list[dict[str, Any]]:  # fmt: skip
    site = await get_owned(s, Site, site_id, auth.org)
    return await energy.site_daily(s, auth.org, site, start, end)
