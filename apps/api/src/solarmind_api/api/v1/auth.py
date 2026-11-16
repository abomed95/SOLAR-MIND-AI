"""Authentication endpoints."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api import schemas as sc
from solarmind_api.deps import SESSION_COOKIE, AppSettings, Auth, SystemDB, client_ip, rate_limit
from solarmind_api.enums import Role
from solarmind_api.errors import NotFound
from solarmind_api.models import Membership, Organization, Session, User
from solarmind_api.permissions import permissions_for
from solarmind_api.security import totp_uri
from solarmind_api.services import identity

router = APIRouter(prefix="/auth", tags=["auth"])
_limited = [Depends(rate_limit("auth", "rate_limit_auth_per_minute"))]


def _set_cookie(response: Response, request: Request, token: str, session: Session) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )


async def build_me(
    s: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID | None
) -> dict[str, Any]:
    user = await s.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    rows = (
        await s.execute(
            select(Membership.organization_id, Organization.name, Membership.role)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(Membership.user_id == user_id, Organization.is_active.is_(True))
            .order_by(Organization.name)
        )
    ).all()
    memberships = [
        sc.MembershipSummary(organization_id=o, organization_name=n, role=Role(r))
        for o, n, r in rows
    ]
    role = next((m.role for m in memberships if m.organization_id == organization_id), None)
    organization = await s.get(Organization, organization_id) if role else None
    return {
        "user": sc.UserOut.model_validate(user),
        "organization": sc.OrganizationOut.model_validate(organization) if organization else None,
        "role": role,
        "permissions": sorted(permissions_for(role)) if role else [],
        "memberships": memberships,
    }


async def _login_response(
    s: AsyncSession, request: Request, response: Response, token: str, session: Session, user: User
) -> sc.LoginOut:
    _set_cookie(response, request, token, session)
    me = await build_me(s, user.id, session.organization_id)
    return sc.LoginOut(token=token, expires_at=session.expires_at, **me)


@router.post("/register", response_model=sc.LoginOut, status_code=201, dependencies=_limited)
async def register(
    data: sc.RegisterIn, request: Request, response: Response, s: SystemDB, settings: AppSettings
) -> sc.LoginOut:
    token, session, user = await identity.register(
        s, settings, organization_name=data.organization_name, full_name=data.full_name,
        email=data.email, password=data.password, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )  # fmt: skip
    return await _login_response(s, request, response, token, session, user)


@router.post("/login", response_model=sc.LoginOut, dependencies=_limited)
async def login(
    data: sc.LoginIn, request: Request, response: Response, s: SystemDB, settings: AppSettings
) -> sc.LoginOut:
    token, session, user = await identity.login(
        s, settings, request.app.state.secret_box, email=data.email, password=data.password,
        totp_code=data.totp_code, organization_id=data.organization_id, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )  # fmt: skip
    return await _login_response(s, request, response, token, session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(auth: Auth, response: Response, s: SystemDB) -> None:
    session = await s.get(Session, auth.session_id)
    if session is not None:
        session.revoked_at = datetime.now(UTC)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=sc.MeOut)
async def me(auth: Auth, s: SystemDB) -> Any:
    return await build_me(s, auth.user_id, auth.organization_id)


@router.post("/switch-organization", response_model=sc.MeOut)
async def switch_organization(data: sc.SwitchOrganizationIn, auth: Auth, s: SystemDB) -> Any:
    await identity.switch_organization(s, auth.user_id, auth.session_id, data.organization_id)
    return await build_me(s, auth.user_id, data.organization_id)


# --------------------------------------------------------------------------- sessions


@router.get("/sessions", response_model=list[sc.SessionOut])
async def list_sessions(auth: Auth, s: SystemDB) -> list[sc.SessionOut]:
    rows = await s.scalars(
        select(Session)
        .where(
            Session.user_id == auth.user_id,
            Session.revoked_at.is_(None),
            Session.expires_at > datetime.now(UTC),
        )
        .order_by(Session.created_at.desc())
    )
    return [
        sc.SessionOut.model_validate(r).model_copy(update={"current": r.id == auth.session_id})
        for r in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: uuid.UUID, auth: Auth, s: SystemDB) -> None:
    session = await s.scalar(
        select(Session).where(Session.id == session_id, Session.user_id == auth.user_id)
    )
    if session is None:
        raise NotFound("Session not found")
    session.revoked_at = datetime.now(UTC)


@router.post("/sessions/revoke-others", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_other_sessions(auth: Auth, s: SystemDB) -> None:
    await identity.revoke_sessions(s, auth.user_id, except_id=auth.session_id)


# --------------------------------------------------------------------------- passwords


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED, dependencies=_limited)
async def forgot_password(
    data: sc.ForgotPasswordIn, request: Request, s: SystemDB, settings: AppSettings
) -> dict[str, str]:
    await identity.request_password_reset(s, settings, request.app.state.mailer, data.email)
    # Identical answer whether the address is registered or not.
    return {"status": "accepted"}


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT, dependencies=_limited)
async def reset_password(data: sc.ResetPasswordIn, s: SystemDB) -> None:
    await identity.reset_password(s, data.token, data.new_password)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(data: sc.ChangePasswordIn, auth: Auth, s: SystemDB) -> None:
    await identity.change_password(
        s, auth.user_id, auth.session_id, data.current_password, data.new_password
    )


# --------------------------------------------------------------------------- MFA


@router.post("/mfa/setup", response_model=sc.MfaSetupOut)
async def mfa_setup(auth: Auth, request: Request, s: SystemDB) -> sc.MfaSetupOut:
    secret, email = await identity.mfa_setup(s, request.app.state.secret_box, auth.user_id)
    return sc.MfaSetupOut(secret=secret, otpauth_uri=totp_uri(secret, email))


@router.post("/mfa/enable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_enable(data: sc.MfaCodeIn, auth: Auth, request: Request, s: SystemDB) -> None:
    await identity.mfa_enable(s, request.app.state.secret_box, auth.user_id, data.code)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(data: sc.MfaDisableIn, auth: Auth, request: Request, s: SystemDB) -> None:
    await identity.mfa_disable(
        s, request.app.state.secret_box, auth.user_id, data.password, data.code
    )


# --------------------------------------------------------------------------- invitations


@router.post(
    "/invitations/accept", response_model=sc.LoginOut, status_code=201, dependencies=_limited
)
async def accept_invitation(
    data: sc.AcceptInvitationIn, request: Request, response: Response, s: SystemDB,
    settings: AppSettings,
) -> sc.LoginOut:  # fmt: skip
    token, session, user = await identity.accept_invitation(
        s, settings, token=data.token, full_name=data.full_name, password=data.password,
        ip=client_ip(request), user_agent=request.headers.get("user-agent"),
    )  # fmt: skip
    return await _login_response(s, request, response, token, session, user)
