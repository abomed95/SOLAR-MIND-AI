"""Accounts, sessions, organizations, invitations, password reset and MFA.

All functions take an open system transaction: authentication necessarily runs
before any tenant context exists.
"""

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.config import Settings
from solarmind_api.enums import Plan, Role
from solarmind_api.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized
from solarmind_api.models import (
    Invitation,
    Membership,
    Organization,
    PasswordReset,
    Session,
    User,
)
from solarmind_api.security import (
    SecretBox,
    hash_password,
    hash_token,
    new_token,
    new_totp_secret,
    verify_password,
    verify_totp,
)
from solarmind_api.services import billing
from solarmind_api.services.audit import audit
from solarmind_api.services.mail import Mailer


def _now() -> datetime:
    return datetime.now(UTC)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return slug or "org"


# --------------------------------------------------------------------------- organizations


async def provision_organization(
    s: AsyncSession,
    settings: Settings,
    name: str,
    *,
    plan: Plan | None = None,
    is_demo: bool = False,
) -> Organization:
    """Create an organization with its subscription and default alert rules."""
    from solarmind_api.services import alerts  # local import: alerts depends on models only

    slug = _slugify(name)
    if await s.scalar(select(Organization.id).where(Organization.slug == slug)):
        slug = f"{slug}-{secrets.token_hex(3)}"
    org = Organization(name=name, slug=slug, is_demo=is_demo)
    s.add(org)
    await s.flush()
    sub = billing.new_trial(org.id, settings.trial_days)
    if plan is not None:
        sub.plan = plan
    s.add(sub)
    alerts.add_default_rules(s, org.id)
    return org


# --------------------------------------------------------------------------- sessions


async def create_session(
    s: AsyncSession,
    settings: Settings,
    user: User,
    organization_id: uuid.UUID | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, Session]:
    token = new_token()
    session = Session(
        user_id=user.id,
        organization_id=organization_id,
        token_hash=hash_token(token),
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
        expires_at=_now() + timedelta(hours=settings.session_ttl_hours),
    )
    s.add(session)
    user.last_login_at = _now()
    await s.flush()
    return token, session


async def revoke_sessions(
    s: AsyncSession, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None
) -> None:
    stmt = (
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    if except_id is not None:
        stmt = stmt.where(Session.id != except_id)
    await s.execute(stmt)


# --------------------------------------------------------------------------- register / login


async def register(
    s: AsyncSession, settings: Settings, *, organization_name: str, full_name: str, email: str,
    password: str, ip: str | None, user_agent: str | None,
) -> tuple[str, Session, User]:  # fmt: skip
    if await s.scalar(select(User.id).where(User.email == email)):
        raise Conflict("An account already exists for this e-mail", code="email_taken")
    user = User(email=email, full_name=full_name, password_hash=hash_password(password))
    s.add(user)
    org = await provision_organization(s, settings, organization_name)
    await s.flush()
    s.add(Membership(organization_id=org.id, user_id=user.id, role=Role.ORGANIZATION_ADMIN))
    audit(s, action="organization.register", organization_id=org.id, actor_id=user.id,
          entity_type="organization", entity_id=org.id, ip=ip)  # fmt: skip
    token, session = await create_session(s, settings, user, org.id, ip, user_agent)
    return token, session, user


async def login(
    s: AsyncSession, settings: Settings, box: SecretBox, *, email: str, password: str,
    totp_code: str | None, organization_id: uuid.UUID | None, ip: str | None,
    user_agent: str | None,
) -> tuple[str, Session, User]:  # fmt: skip
    user = await s.scalar(select(User).where(User.email == email))
    # Same code path and message whether the account exists or not.
    if not verify_password(user.password_hash if user else None, password) or user is None:
        audit(s, action="auth.login_failed", organization_id=None, actor_id=None,
              details={"email": email}, ip=ip)  # fmt: skip
        await s.commit()
        raise Unauthorized("Invalid e-mail or password", code="invalid_credentials")
    if not user.is_active:
        raise Unauthorized("Account disabled", code="account_disabled")

    if user.mfa_enabled:
        if totp_code is None:
            raise Unauthorized("MFA code required", code="mfa_required")
        secret = box.decrypt(user.mfa_secret_encrypted or "")
        if secret is None or not verify_totp(secret, totp_code):
            audit(s, action="auth.mfa_failed", organization_id=None, actor_id=user.id, ip=ip)
            await s.commit()
            raise Unauthorized("Invalid MFA code", code="mfa_invalid")

    memberships = (
        (
            await s.execute(
                select(Membership.organization_id)
                .join(Organization, Organization.id == Membership.organization_id)
                .where(Membership.user_id == user.id, Organization.is_active.is_(True))
                .order_by(Membership.created_at)
            )
        )
        .scalars()
        .all()
    )
    if organization_id is not None and organization_id not in memberships:
        raise Forbidden("Not a member of this organization")
    active_org = organization_id or (memberships[0] if memberships else None)
    if active_org is None and not user.is_super_admin:
        raise Forbidden("This account belongs to no active organization", code="no_organization")

    token, session = await create_session(s, settings, user, active_org, ip, user_agent)
    audit(s, action="auth.login", organization_id=active_org, actor_id=user.id, ip=ip)
    return token, session, user


async def switch_organization(
    s: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, organization_id: uuid.UUID
) -> None:
    member = await s.scalar(
        select(Membership.id)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(
            Membership.user_id == user_id,
            Membership.organization_id == organization_id,
            Organization.is_active.is_(True),
        )
    )
    if member is None:
        raise Forbidden("Not a member of this organization")
    await s.execute(
        update(Session).where(Session.id == session_id).values(organization_id=organization_id)
    )


# --------------------------------------------------------------------------- passwords


async def request_password_reset(
    s: AsyncSession, settings: Settings, mailer: Mailer, email: str
) -> None:
    """Always succeeds from the caller's point of view (no account enumeration)."""
    user = await s.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if user is None:
        return
    token = new_token()
    s.add(
        PasswordReset(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=_now() + timedelta(minutes=settings.password_reset_ttl_minutes),
        )
    )
    audit(s, action="auth.password_reset_requested", organization_id=None, actor_id=user.id)
    await mailer.send(
        user.email,
        "SOLAR MIND AI - password reset",
        f"Reset your password: {settings.web_base_url}/reset-password?token={token}\n"
        f"This link expires in {settings.password_reset_ttl_minutes} minutes.",
    )


async def reset_password(s: AsyncSession, token: str, new_password: str) -> None:
    reset = await s.scalar(
        select(PasswordReset).where(PasswordReset.token_hash == hash_token(token)).with_for_update()
    )
    if reset is None or reset.used_at is not None or reset.expires_at <= _now():
        raise AppError("Invalid or expired reset link", code="invalid_token")
    user = await s.get(User, reset.user_id)
    if user is None:
        raise AppError("Invalid or expired reset link", code="invalid_token")
    user.password_hash = hash_password(new_password)
    reset.used_at = _now()
    # A password reset ends every existing session.
    await revoke_sessions(s, user.id)
    audit(s, action="auth.password_reset", organization_id=None, actor_id=user.id)


async def change_password(
    s: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, current: str, new: str
) -> None:
    user = await s.get(User, user_id)
    if user is None or not verify_password(user.password_hash, current):
        raise Unauthorized("Current password is incorrect", code="invalid_credentials")
    user.password_hash = hash_password(new)
    await revoke_sessions(s, user.id, except_id=session_id)
    audit(s, action="auth.password_changed", organization_id=None, actor_id=user.id)


# --------------------------------------------------------------------------- MFA


async def mfa_setup(s: AsyncSession, box: SecretBox, user_id: uuid.UUID) -> tuple[str, str]:
    user = await s.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    if user.mfa_enabled:
        raise Conflict("MFA is already enabled")
    secret = new_totp_secret()
    user.mfa_secret_encrypted = box.encrypt(secret)
    return secret, user.email


async def mfa_enable(s: AsyncSession, box: SecretBox, user_id: uuid.UUID, code: str) -> None:
    user = await s.get(User, user_id)
    secret = box.decrypt(user.mfa_secret_encrypted or "") if user else None
    if user is None or secret is None:
        raise AppError("Start MFA setup first", code="mfa_not_setup")
    if not verify_totp(secret, code):
        raise AppError("Invalid MFA code", code="mfa_invalid")
    user.mfa_enabled = True
    audit(s, action="auth.mfa_enabled", organization_id=None, actor_id=user.id)


async def mfa_disable(
    s: AsyncSession, box: SecretBox, user_id: uuid.UUID, password: str, code: str
) -> None:
    user = await s.get(User, user_id)
    if user is None or not verify_password(user.password_hash, password):
        raise Unauthorized("Password is incorrect", code="invalid_credentials")
    secret = box.decrypt(user.mfa_secret_encrypted or "")
    if not user.mfa_enabled or secret is None or not verify_totp(secret, code):
        raise AppError("Invalid MFA code", code="mfa_invalid")
    user.mfa_enabled, user.mfa_secret_encrypted = False, None
    audit(s, action="auth.mfa_disabled", organization_id=None, actor_id=user.id)


# --------------------------------------------------------------------------- invitations


async def create_invitation(
    s: AsyncSession, settings: Settings, mailer: Mailer, *, organization_id: uuid.UUID,
    organization_name: str, email: str, role: Role, invited_by: uuid.UUID | None,
) -> tuple[Invitation, str]:  # fmt: skip
    already_member = await s.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.organization_id == organization_id, User.email == email)
    )
    if already_member:
        raise Conflict("This person is already a member", code="already_member")
    pending = await s.scalar(
        select(Invitation).where(
            Invitation.organization_id == organization_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > _now(),
        )
    )
    if pending is not None:
        raise Conflict("An invitation is already pending for this e-mail", code="already_invited")

    token = new_token()
    invitation = Invitation(
        organization_id=organization_id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        invited_by=invited_by,
        expires_at=_now() + timedelta(hours=settings.invitation_ttl_hours),
    )
    s.add(invitation)
    await s.flush()
    url = f"{settings.web_base_url}/accept-invitation?token={token}"
    audit(s, action="invitation.create", organization_id=organization_id, actor_id=invited_by,
          entity_type="invitation", entity_id=invitation.id, details={"email": email, "role": role})  # fmt: skip
    await mailer.send(
        email,
        f"SOLAR MIND AI - invitation to {organization_name}",
        f"You have been invited to join {organization_name} as {role}.\nAccept: {url}\n"
        f"This link expires in {settings.invitation_ttl_hours} hours.",
    )
    return invitation, url


async def accept_invitation(
    s: AsyncSession, settings: Settings, *, token: str, full_name: str, password: str,
    ip: str | None, user_agent: str | None,
) -> tuple[str, Session, User]:  # fmt: skip
    invitation = await s.scalar(
        select(Invitation).where(Invitation.token_hash == hash_token(token)).with_for_update()
    )
    if invitation is None or invitation.accepted_at is not None or invitation.expires_at <= _now():
        raise AppError("Invalid or expired invitation", code="invalid_token")

    user = await s.scalar(select(User).where(User.email == invitation.email))
    if user is None:
        user = User(
            email=invitation.email, full_name=full_name, password_hash=hash_password(password)
        )
        s.add(user)
        await s.flush()
    elif not verify_password(user.password_hash, password):
        # Existing account: joining another organization requires its password.
        raise Unauthorized("Invalid password for the existing account", code="invalid_credentials")

    s.add(
        Membership(
            organization_id=invitation.organization_id, user_id=user.id, role=invitation.role
        )
    )
    invitation.accepted_at = _now()
    audit(s, action="invitation.accept", organization_id=invitation.organization_id,
          actor_id=user.id, entity_type="invitation", entity_id=invitation.id, ip=ip)  # fmt: skip
    token_out, session = await create_session(
        s, settings, user, invitation.organization_id, ip, user_agent
    )
    return token_out, session, user


# --------------------------------------------------------------------------- members


async def count_admins(s: AsyncSession, organization_id: uuid.UUID) -> int:
    return (
        await s.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.organization_id == organization_id,
                Membership.role == Role.ORGANIZATION_ADMIN,
            )
        )
        or 0
    )
