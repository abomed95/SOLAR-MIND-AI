"""Current organization, its members, invitations and audit trail."""

import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import select, update

from solarmind_api import schemas as sc
from solarmind_api.deps import (
    AppSettings,
    AuthContext,
    Page,
    Paging,
    Perm,
    TenantDB,
    client_ip,
    get_owned,
    page,
    paginate,
)
from solarmind_api.enums import Role
from solarmind_api.errors import Conflict, NotFound
from solarmind_api.models import AuditLog, Invitation, Membership, Organization, Session, User
from solarmind_api.permissions import P
from solarmind_api.services import billing, identity
from solarmind_api.services.audit import audit

org_router = APIRouter(prefix="/organizations", tags=["organizations"])
users_router = APIRouter(prefix="/users", tags=["users"])


@org_router.get("/current", response_model=sc.OrganizationOut)
async def get_current(s: TenantDB, auth: AuthContext = Perm()) -> Any:
    return await s.get(Organization, auth.org)


@org_router.patch("/current", response_model=sc.OrganizationOut)
async def update_current(
    data: sc.OrganizationUpdate, request: Request, s: TenantDB, auth: AuthContext = Perm(P.ORG_MANAGE)
) -> Any:
    org = await s.get(Organization, auth.org)
    if org is None:
        raise NotFound("Organization not found")
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(org, key, value)
    audit(s, action="organization.update", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="organization", entity_id=org.id, details=changes, ip=client_ip(request))  # fmt: skip
    return org


@org_router.get("/current/audit-logs", response_model=Page[sc.AuditLogOut])
async def audit_logs(
    s: TenantDB, params: Paging, auth: AuthContext = Perm(P.AUDIT_READ), action: str | None = None
) -> Any:
    stmt = select(AuditLog).where(AuditLog.organization_id == auth.org)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    items, total = await paginate(s, stmt, params, {"created_at": AuditLog.created_at}, "-created_at")
    return page(items, total, params)


# --------------------------------------------------------------------------- members


@users_router.get("", response_model=list[sc.MemberOut])
async def list_members(s: TenantDB, auth: AuthContext = Perm(P.USERS_READ)) -> list[sc.MemberOut]:
    rows = await s.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == auth.org)
        .order_by(User.full_name)
    )
    return [
        sc.MemberOut(user_id=u.id, email=u.email, full_name=u.full_name, role=Role(m.role),
                     mfa_enabled=u.mfa_enabled, joined_at=m.created_at)  # fmt: skip
        for u, m in rows
    ]


async def _membership(s: TenantDB, org: uuid.UUID, user_id: uuid.UUID) -> Membership:
    membership = await s.scalar(
        select(Membership).where(Membership.organization_id == org, Membership.user_id == user_id)
    )
    if membership is None:
        raise NotFound("Member not found")
    return membership


@users_router.patch("/{user_id}", response_model=sc.MemberOut)
async def update_member(
    user_id: uuid.UUID, data: sc.MemberUpdate, request: Request, s: TenantDB,
    auth: AuthContext = Perm(P.USERS_MANAGE),
) -> sc.MemberOut:  # fmt: skip
    membership = await _membership(s, auth.org, user_id)
    if (
        membership.role == Role.ORGANIZATION_ADMIN
        and data.role != Role.ORGANIZATION_ADMIN
        and await identity.count_admins(s, auth.org) <= 1
    ):
        raise Conflict("An organization needs at least one administrator", code="last_admin")
    previous, membership.role = membership.role, data.role
    audit(s, action="member.role_change", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="user", entity_id=user_id, details={"from": previous, "to": data.role},
          ip=client_ip(request))  # fmt: skip
    user = await s.get(User, user_id)
    assert user is not None  # noqa: S101 - guaranteed by the membership FK
    return sc.MemberOut(user_id=user.id, email=user.email, full_name=user.full_name,
                        role=data.role, mfa_enabled=user.mfa_enabled,
                        joined_at=membership.created_at)  # fmt: skip


@users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.USERS_MANAGE)
) -> None:
    membership = await _membership(s, auth.org, user_id)
    if (
        membership.role == Role.ORGANIZATION_ADMIN
        and await identity.count_admins(s, auth.org) <= 1
    ):
        raise Conflict("An organization needs at least one administrator", code="last_admin")
    await s.delete(membership)
    audit(s, action="member.remove", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="user", entity_id=user_id, ip=client_ip(request))  # fmt: skip
    # Sessions of that user pointing at this organization lose it at once
    # (deps.get_auth re-checks membership on every request); detach them too.
    async with request.app.state.db.system() as system:
        await system.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.organization_id == auth.org)
            .values(organization_id=None)
        )


# --------------------------------------------------------------------------- invitations


@users_router.get("/invitations", response_model=list[sc.InvitationOut])
async def list_invitations(s: TenantDB, auth: AuthContext = Perm(P.USERS_MANAGE)) -> Any:
    rows = await s.scalars(
        select(Invitation)
        .where(Invitation.organization_id == auth.org)
        .order_by(Invitation.created_at.desc())
    )
    return rows.all()


@users_router.post("/invitations", response_model=sc.InvitationCreated, status_code=201)
async def invite(
    data: sc.InvitationCreate, request: Request, s: TenantDB, settings: AppSettings,
    auth: AuthContext = Perm(P.USERS_MANAGE),
) -> sc.InvitationCreated:  # fmt: skip
    await billing.ensure_can_add(s, auth.org, "users")
    org = await s.get(Organization, auth.org)
    assert org is not None  # noqa: S101
    invitation, url = await identity.create_invitation(
        s, settings, request.app.state.mailer, organization_id=auth.org,
        organization_name=org.name, email=data.email, role=data.role, invited_by=auth.user_id,
    )  # fmt: skip
    return sc.InvitationCreated(
        **sc.InvitationOut.model_validate(invitation).model_dump(), invitation_url=url
    )


@users_router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invitation(
    invitation_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.USERS_MANAGE)
) -> None:
    invitation = await get_owned(s, Invitation, invitation_id, auth.org)
    if invitation.accepted_at is not None:
        raise Conflict("Invitation already accepted")
    await s.delete(invitation)
