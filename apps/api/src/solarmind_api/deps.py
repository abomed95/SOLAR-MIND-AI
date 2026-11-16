"""Request dependencies: authentication, authorization, DB sessions, paging, rate limits.

Authorization is decided here and in the services, never by the front end.
"""

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar

from fastapi import Depends, Query, Request
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from solarmind_api.config import Settings
from solarmind_api.enums import Role
from solarmind_api.errors import (
    AppError,
    Forbidden,
    NotFound,
    RateLimited,
    ServiceUnavailable,
    Unauthorized,
)
from solarmind_api.models import Membership, Organization, Session, User
from solarmind_api.permissions import P, permissions_for
from solarmind_api.security import hash_token

SESSION_COOKIE = "sm_session"
CSRF_HEADER = "x-solarmind-csrf"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    session_id: uuid.UUID
    email: str
    is_super_admin: bool
    mfa_enabled: bool
    organization_id: uuid.UUID | None
    role: Role | None

    @property
    def permissions(self) -> frozenset[P]:
        return permissions_for(self.role) if self.role else frozenset()

    @property
    def org(self) -> uuid.UUID:
        if self.organization_id is None:
            raise Forbidden("No organization selected", code="no_organization")
        return self.organization_id


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_auth(request: Request) -> AuthContext:
    token: str | None = None
    via_cookie = False
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    elif SESSION_COOKIE in request.cookies:
        token, via_cookie = request.cookies[SESSION_COOKIE], True
    if not token:
        raise Unauthorized("Authentication required")
    # CSRF: a cross-site form cannot set a custom header, and a cross-origin
    # fetch that does would need a CORS preflight, which this API never grants.
    if via_cookie and request.method not in _SAFE_METHODS:
        if request.headers.get(CSRF_HEADER) != "1":
            raise Forbidden("Missing CSRF header", code="csrf")

    async with request.app.state.db.system() as s:
        row = (
            await s.execute(
                select(Session, User)
                .join(User, User.id == Session.user_id)
                .where(Session.token_hash == hash_token(token))
            )
        ).first()
        if row is None:
            raise Unauthorized("Invalid session")
        session, user = row
        if session.revoked_at is not None or session.expires_at <= datetime.now(UTC):
            raise Unauthorized("Session expired", code="session_expired")
        if not user.is_active:
            raise Unauthorized("Account disabled")

        organization_id, role = None, None
        if session.organization_id is not None:
            membership = (
                await s.execute(
                    select(Membership.role)
                    .join(Organization, Organization.id == Membership.organization_id)
                    .where(
                        Membership.organization_id == session.organization_id,
                        Membership.user_id == user.id,
                        Organization.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            # Membership removed or organization suspended since login: the
            # session stays valid but loses its organization.
            if membership is not None:
                organization_id, role = session.organization_id, Role(membership)

    return AuthContext(
        user_id=user.id,
        session_id=session.id,
        email=user.email,
        is_super_admin=user.is_super_admin,
        mfa_enabled=user.mfa_enabled,
        organization_id=organization_id,
        role=role,
    )


def require(*permissions: P) -> Callable[..., Awaitable[AuthContext]]:
    async def dependency(auth: Annotated[AuthContext, Depends(get_auth)]) -> AuthContext:
        _ = auth.org
        if not set(permissions) <= auth.permissions:
            raise Forbidden("Insufficient permissions")
        return auth

    return dependency


def Perm(*permissions: P) -> Any:  # noqa: N802 - reads like a FastAPI marker
    return Depends(require(*permissions))


async def require_super_admin(
    request: Request, auth: Annotated[AuthContext, Depends(get_auth)]
) -> AuthContext:
    if not auth.is_super_admin:
        raise Forbidden("Platform administrator only")
    settings: Settings = request.app.state.settings
    # Platform administration is the most sensitive surface: MFA is mandatory
    # outside development and test.
    if settings.environment in ("staging", "production") and not auth.mfa_enabled:
        raise Forbidden("MFA must be enabled for platform administration", code="mfa_required")
    return auth


async def tenant_session(
    request: Request, auth: Annotated[AuthContext, Depends(get_auth)]
) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.tenant(auth.org) as session:
        yield session


async def system_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.system() as session:
        yield session


Auth = Annotated[AuthContext, Depends(get_auth)]
SuperAdmin = Annotated[AuthContext, Depends(require_super_admin)]
TenantDB = Annotated[AsyncSession, Depends(tenant_session)]
SystemDB = Annotated[AsyncSession, Depends(system_session)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]


# --------------------------------------------------------------------------- rate limiting


def rate_limit(scope: str, setting: str) -> Callable[[Request], Awaitable[None]]:
    """Fixed one-minute window per client IP, counted in Redis."""

    async def dependency(request: Request) -> None:
        limit: int = getattr(request.app.state.settings, setting)
        key = f"rl:{scope}:{client_ip(request)}:{int(time.time()) // 60}"
        try:
            count = await request.app.state.redis.incr(key)
            if count == 1:
                await request.app.state.redis.expire(key, 120)
        except RedisError as exc:
            # Fail closed where brute force matters, open elsewhere.
            if scope == "auth":
                raise ServiceUnavailable("Rate limiter unavailable") from exc
            return
        if count > limit:
            raise RateLimited("Too many requests, retry in a minute")

    return dependency


# --------------------------------------------------------------------------- pagination

T = TypeVar("T")


class PageParams:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: Annotated[str | None, Query(max_length=40)] = None,
    ) -> None:
        self.limit, self.offset, self.sort = limit, offset, sort


Paging = Annotated[PageParams, Depends()]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    params: PageParams,
    sortable: dict[str, InstrumentedAttribute[Any]],
    default_sort: str,
) -> tuple[list[Any], int]:
    """Run ``stmt`` with whitelisted sorting (``field`` or ``-field``) and paging."""
    sort = params.sort or default_sort
    descending, field = sort.startswith("-"), sort.lstrip("-")
    if field not in sortable:
        raise AppError(
            f"Cannot sort by '{field}'",
            code="invalid_sort",
            status_code=422,
            details={"allowed": sorted(sortable)},
        )
    column = sortable[field]
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.scalars(
        stmt.order_by(column.desc() if descending else column.asc())
        .limit(params.limit)
        .offset(params.offset)
    )
    return list(rows.all()), total


def page(items: list[Any], total: int, params: PageParams) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": params.limit, "offset": params.offset}


async def get_owned(session: AsyncSession, model: Any, entity_id: uuid.UUID, org: uuid.UUID) -> Any:
    """Fetch a row of the caller's organization or raise 404.

    The explicit filter is the first isolation layer; RLS is the second. A row
    of another organization is reported as missing, never as forbidden, so its
    existence is not disclosed.
    """
    obj = await session.scalar(
        select(model).where(model.id == entity_id, model.organization_id == org)
    )
    if obj is None:
        raise NotFound(f"{model.__name__} not found")
    return obj
