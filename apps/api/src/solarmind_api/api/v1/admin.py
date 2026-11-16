"""Platform administration (SUPER_ADMIN only, cross-organization by design)."""

import uuid
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from solarmind_api import schemas as sc
from solarmind_api.deps import AppSettings, Page, Paging, SuperAdmin, SystemDB, client_ip, page
from solarmind_api.deps import paginate
from solarmind_api.enums import BillingStatus, Role
from solarmind_api.errors import NotFound
from solarmind_api.models import BillingRecord, Device, Organization, Site, Subscription, User
from solarmind_api.services import identity
from solarmind_api.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
async def stats(_: SuperAdmin, s: SystemDB) -> dict[str, int]:
    async def count(model: type) -> int:
        return await s.scalar(select(func.count()).select_from(model)) or 0

    return {
        "organizations": await count(Organization),
        "users": await count(User),
        "sites": await count(Site),
        "devices": await count(Device),
    }


@router.get("/organizations", response_model=Page[sc.OrganizationOut])
async def list_organizations(_: SuperAdmin, s: SystemDB, params: Paging) -> Any:
    sortable = {"name": Organization.name, "created_at": Organization.created_at}
    items, total = await paginate(s, select(Organization), params, sortable, "name")
    return page(items, total, params)


@router.post("/organizations", response_model=sc.InvitationCreated, status_code=201)
async def create_organization(
    data: sc.OrganizationCreate, request: Request, auth: SuperAdmin, s: SystemDB,
    settings: AppSettings,
) -> Any:  # fmt: skip
    """Create an organization and invite its first administrator."""
    org = await identity.provision_organization(s, settings, data.name, plan=data.plan)
    audit(s, action="organization.create", organization_id=org.id, actor_id=auth.user_id,
          entity_type="organization", entity_id=org.id, ip=client_ip(request))  # fmt: skip
    invitation, url = await identity.create_invitation(
        s, settings, request.app.state.mailer, organization_id=org.id, organization_name=org.name,
        email=data.admin_email, role=Role.ORGANIZATION_ADMIN, invited_by=auth.user_id,
    )  # fmt: skip
    return sc.InvitationCreated(
        **sc.InvitationOut.model_validate(invitation).model_dump(), invitation_url=url
    )


@router.patch("/organizations/{organization_id}", response_model=sc.OrganizationOut)
async def set_organization_active(
    organization_id: uuid.UUID, data: sc.OrganizationAdminUpdate, request: Request,
    auth: SuperAdmin, s: SystemDB,
) -> Any:  # fmt: skip
    org = await s.get(Organization, organization_id)
    if org is None:
        raise NotFound("Organization not found")
    org.is_active = data.is_active
    audit(s, action="organization.set_active", organization_id=org.id, actor_id=auth.user_id,
          entity_type="organization", entity_id=org.id, details={"is_active": data.is_active},
          ip=client_ip(request))  # fmt: skip
    return org


@router.get("/subscriptions")
async def list_subscriptions(_: SuperAdmin, s: SystemDB) -> list[dict[str, Any]]:
    rows = await s.execute(
        select(Organization.id, Organization.name, Subscription)
        .join(Subscription, Subscription.organization_id == Organization.id)
        .order_by(Organization.name)
    )
    return [
        {"organization_id": org_id, "organization_name": name, "plan": sub.plan,
         "status": sub.status, "billing_cycle": sub.billing_cycle,
         "current_period_end": sub.current_period_end}  # fmt: skip
        for org_id, name, sub in rows
    ]


@router.post("/billing-records/{record_id}/mark-paid", response_model=sc.BillingRecordOut)
async def mark_paid(record_id: uuid.UUID, request: Request, auth: SuperAdmin, s: SystemDB) -> Any:
    """Record an out-of-band settlement (bank transfer, purchase order)."""
    record = await s.get(BillingRecord, record_id)
    if record is None:
        raise NotFound("Billing record not found")
    record.status = BillingStatus.PAID
    audit(s, action="billing.mark_paid", organization_id=record.organization_id,
          actor_id=auth.user_id, entity_type="billing_record", entity_id=record.id,
          ip=client_ip(request))  # fmt: skip
    return record
