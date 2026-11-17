"""Sites, assets, devices and gateways."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from solarmind_api import schemas as sc
from solarmind_api.deps import (
    AuthContext,
    BustCache,
    Page,
    Paging,
    Perm,
    TenantDB,
    client_ip,
    get_owned,
    page,
    paginate,
)
from solarmind_api.enums import AssetKind, SiteKind
from solarmind_api.models import Asset, Device, Gateway, Site
from solarmind_api.permissions import P
from solarmind_api.security import hash_token, new_token
from solarmind_api.services import billing
from solarmind_api.services.audit import audit

sites = APIRouter(prefix="/sites", tags=["sites"])
assets = APIRouter(prefix="/assets", tags=["assets"])
devices = APIRouter(prefix="/devices", tags=["devices"])
gateways = APIRouter(prefix="/gateways", tags=["gateways"])

# A device or gateway is OFFLINE after this many missed sampling intervals.
MISSED_INTERVALS_BEFORE_OFFLINE = 3


def connection_status(last_seen: datetime | None, interval_s: int, now: datetime) -> str:
    if last_seen is None:
        return "NEVER_SEEN"
    limit = timedelta(seconds=interval_s * MISSED_INTERVALS_BEFORE_OFFLINE)
    return "ONLINE" if now - last_seen <= limit else "OFFLINE"


def _apply(obj: Any, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        setattr(obj, key, value)


# --------------------------------------------------------------------------- sites


@sites.get("", response_model=Page[sc.SiteOut])
async def list_sites(
    s: TenantDB, params: Paging, auth: AuthContext = Perm(P.SITES_READ),
    kind: SiteKind | None = None, q: str | None = None,
) -> Any:  # fmt: skip
    stmt = select(Site).where(Site.organization_id == auth.org)
    if kind:
        stmt = stmt.where(Site.kind == kind)
    if q:
        stmt = stmt.where(Site.name.ilike(f"%{q}%"))
    sortable = {"name": Site.name, "created_at": Site.created_at,
                "installed_capacity_kwp": Site.installed_capacity_kwp}  # fmt: skip
    items, total = await paginate(s, stmt, params, sortable, "name")
    return page(items, total, params)


@sites.post("", response_model=sc.SiteOut, status_code=201, dependencies=[BustCache])
async def create_site(
    data: sc.SiteCreate, request: Request, s: TenantDB, auth: AuthContext = Perm(P.SITES_WRITE)
) -> Any:
    await billing.ensure_can_add(s, auth.org, "sites")
    site = Site(organization_id=auth.org, **data.model_dump())
    s.add(site)
    await s.flush()
    audit(s, action="site.create", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="site", entity_id=site.id, details={"name": site.name},
          ip=client_ip(request))  # fmt: skip
    await s.refresh(site)
    return site


@sites.get("/{site_id}", response_model=sc.SiteOut)
async def get_site(site_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.SITES_READ)) -> Any:
    return await get_owned(s, Site, site_id, auth.org)


@sites.patch("/{site_id}", response_model=sc.SiteOut, dependencies=[BustCache])
async def update_site(
    site_id: uuid.UUID, data: sc.SiteUpdate, request: Request, s: TenantDB,
    auth: AuthContext = Perm(P.SITES_WRITE),
) -> Any:  # fmt: skip
    site = await get_owned(s, Site, site_id, auth.org)
    changes = data.model_dump(exclude_unset=True)
    _apply(site, changes)
    audit(s, action="site.update", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="site", entity_id=site.id, details={"fields": sorted(changes)},
          ip=client_ip(request))  # fmt: skip
    return site


@sites.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[BustCache])
async def delete_site(
    site_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.SITES_WRITE)
) -> None:
    site = await get_owned(s, Site, site_id, auth.org)
    await s.delete(site)
    audit(s, action="site.delete", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="site", entity_id=site_id, details={"name": site.name},
          ip=client_ip(request))  # fmt: skip


# --------------------------------------------------------------------------- assets


@assets.get("", response_model=Page[sc.AssetOut])
async def list_assets(
    s: TenantDB, params: Paging, auth: AuthContext = Perm(P.SITES_READ),
    site_id: uuid.UUID | None = None, kind: AssetKind | None = None,
) -> Any:  # fmt: skip
    stmt = select(Asset).where(Asset.organization_id == auth.org)
    if site_id:
        stmt = stmt.where(Asset.site_id == site_id)
    if kind:
        stmt = stmt.where(Asset.kind == kind)
    items, total = await paginate(
        s, stmt, params, {"name": Asset.name, "created_at": Asset.created_at}, "name"
    )
    return page(items, total, params)


@assets.post("", response_model=sc.AssetOut, status_code=201)
async def create_asset(
    data: sc.AssetCreate, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> Any:
    await get_owned(s, Site, data.site_id, auth.org)
    asset = Asset(organization_id=auth.org, **data.model_dump())
    s.add(asset)
    await s.flush()
    audit(s, action="asset.create", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="asset", entity_id=asset.id, ip=client_ip(request))  # fmt: skip
    await s.refresh(asset)
    return asset


@assets.patch("/{asset_id}", response_model=sc.AssetOut)
async def update_asset(
    asset_id: uuid.UUID,
    data: sc.AssetUpdate,
    s: TenantDB,
    auth: AuthContext = Perm(P.DEVICES_WRITE),
) -> Any:
    asset = await get_owned(s, Asset, asset_id, auth.org)
    _apply(asset, data.model_dump(exclude_unset=True))
    return asset


@assets.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> None:
    await s.delete(await get_owned(s, Asset, asset_id, auth.org))
    audit(s, action="asset.delete", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="asset", entity_id=asset_id, ip=client_ip(request))  # fmt: skip


# --------------------------------------------------------------------------- devices


async def _intervals(s: TenantDB, org: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = await s.execute(
        select(Site.id, Site.expected_interval_s).where(Site.organization_id == org)
    )
    return {site_id: interval for site_id, interval in rows}


def _device_out(device: Device, interval_s: int, now: datetime) -> sc.DeviceOut:
    out = sc.DeviceOut.model_validate(device)
    out.status = connection_status(device.last_seen_at, interval_s, now)
    return out


async def _check_links(
    s: TenantDB, org: uuid.UUID, data: sc.DeviceCreate | sc.DeviceUpdate
) -> None:
    """Referenced rows must belong to the caller's organization."""
    if data.asset_id:
        await get_owned(s, Asset, data.asset_id, org)
    if data.gateway_id:
        await get_owned(s, Gateway, data.gateway_id, org)


@devices.get("", response_model=Page[sc.DeviceOut])
async def list_devices(
    s: TenantDB, params: Paging, auth: AuthContext = Perm(P.SITES_READ),
    site_id: uuid.UUID | None = None,
) -> Any:  # fmt: skip
    stmt = select(Device).where(Device.organization_id == auth.org)
    if site_id:
        stmt = stmt.where(Device.site_id == site_id)
    items, total = await paginate(
        s, stmt, params, {"name": Device.name, "created_at": Device.created_at,
                          "last_seen_at": Device.last_seen_at}, "name"
    )  # fmt: skip
    intervals, now = await _intervals(s, auth.org), datetime.now(UTC)
    return page([_device_out(d, intervals.get(d.site_id, 300), now) for d in items], total, params)


@devices.post("", response_model=sc.DeviceOut, status_code=201, dependencies=[BustCache])
async def create_device(
    data: sc.DeviceCreate, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> Any:
    site = await get_owned(s, Site, data.site_id, auth.org)
    await _check_links(s, auth.org, data)
    await billing.ensure_can_add(s, auth.org, "devices")
    device = Device(organization_id=auth.org, **data.model_dump())
    s.add(device)
    await s.flush()
    audit(s, action="device.create", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="device", entity_id=device.id, details={"name": device.name},
          ip=client_ip(request))  # fmt: skip
    await s.refresh(device)
    return _device_out(device, site.expected_interval_s, datetime.now(UTC))


@devices.get("/{device_id}", response_model=sc.DeviceOut)
async def get_device(
    device_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.SITES_READ)
) -> Any:
    device = await get_owned(s, Device, device_id, auth.org)
    intervals = await _intervals(s, auth.org)
    return _device_out(device, intervals.get(device.site_id, 300), datetime.now(UTC))


@devices.patch("/{device_id}", response_model=sc.DeviceOut, dependencies=[BustCache])
async def update_device(
    device_id: uuid.UUID, data: sc.DeviceUpdate, s: TenantDB,
    auth: AuthContext = Perm(P.DEVICES_WRITE),
) -> Any:  # fmt: skip
    device = await get_owned(s, Device, device_id, auth.org)
    await _check_links(s, auth.org, data)
    _apply(device, data.model_dump(exclude_unset=True))
    intervals = await _intervals(s, auth.org)
    return _device_out(device, intervals.get(device.site_id, 300), datetime.now(UTC))


@devices.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[BustCache])
async def delete_device(
    device_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> None:
    await s.delete(await get_owned(s, Device, device_id, auth.org))
    audit(s, action="device.delete", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="device", entity_id=device_id, ip=client_ip(request))  # fmt: skip


# --------------------------------------------------------------------------- gateways


def _gateway_out(gateway: Gateway, interval_s: int, now: datetime) -> sc.GatewayOut:
    out = sc.GatewayOut.model_validate(gateway)
    out.status = connection_status(gateway.last_seen_at, interval_s, now)
    return out


@gateways.get("", response_model=list[sc.GatewayOut])
async def list_gateways(
    s: TenantDB, auth: AuthContext = Perm(P.SITES_READ), site_id: uuid.UUID | None = None
) -> Any:
    stmt = select(Gateway).where(Gateway.organization_id == auth.org).order_by(Gateway.name)
    if site_id:
        stmt = stmt.where(Gateway.site_id == site_id)
    intervals, now = await _intervals(s, auth.org), datetime.now(UTC)
    return [_gateway_out(g, intervals.get(g.site_id, 300), now) for g in await s.scalars(stmt)]


@gateways.post("", response_model=sc.GatewayCreated, status_code=201)
async def create_gateway(
    data: sc.GatewayCreate, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> Any:
    await get_owned(s, Site, data.site_id, auth.org)
    api_key = new_token()
    gateway = Gateway(
        organization_id=auth.org, api_key_hash=hash_token(api_key), **data.model_dump()
    )
    s.add(gateway)
    await s.flush()
    audit(s, action="gateway.create", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="gateway", entity_id=gateway.id, ip=client_ip(request))  # fmt: skip
    await s.refresh(gateway)
    return sc.GatewayCreated(
        **sc.GatewayOut.model_validate(gateway).model_dump(exclude={"status"}),
        status="NEVER_SEEN", api_key=api_key,
    )  # fmt: skip


@gateways.post("/{gateway_id}/rotate-key", response_model=sc.GatewayCreated)
async def rotate_gateway_key(
    gateway_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> Any:
    gateway = await get_owned(s, Gateway, gateway_id, auth.org)
    api_key = new_token()
    gateway.api_key_hash = hash_token(api_key)
    audit(s, action="gateway.rotate_key", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="gateway", entity_id=gateway.id, ip=client_ip(request))  # fmt: skip
    return sc.GatewayCreated(**sc.GatewayOut.model_validate(gateway).model_dump(), api_key=api_key)


@gateways.delete("/{gateway_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gateway(
    gateway_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.DEVICES_WRITE)
) -> None:
    await s.delete(await get_owned(s, Gateway, gateway_id, auth.org))
    audit(s, action="gateway.delete", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="gateway", entity_id=gateway_id, ip=client_ip(request))  # fmt: skip
