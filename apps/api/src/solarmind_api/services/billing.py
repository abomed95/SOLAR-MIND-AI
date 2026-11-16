"""Subscriptions, plan limits and feature entitlements.

No payment provider is assumed. ``PaymentProvider`` is the seam for a future
integration; ``ManualPaymentProvider`` records invoices settled out of band.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.enums import BillingCycle, BillingStatus, Plan, SubscriptionStatus
from solarmind_api.errors import NotFound, PlanLimitExceeded
from solarmind_api.models import BillingRecord, Device, Invitation, Membership, Site, Subscription
from solarmind_api.plans import PLANS, Feature, PlanSpec

AccessState = Literal["ACTIVE", "GRACE", "EXPIRED"]
Resource = Literal["sites", "devices", "users"]


@dataclass(frozen=True)
class Entitlements:
    subscription: Subscription
    spec: PlanSpec
    state: AccessState
    grace_until: datetime


def access_state(sub: Subscription, now: datetime) -> tuple[AccessState, datetime]:
    grace_until = sub.current_period_end + timedelta(days=sub.grace_period_days)
    if sub.status != SubscriptionStatus.CANCELLED and now <= sub.current_period_end:
        return "ACTIVE", grace_until
    if now <= grace_until:
        return "GRACE", grace_until
    return "EXPIRED", grace_until


async def get_entitlements(session: AsyncSession, org: uuid.UUID) -> Entitlements:
    sub = await session.scalar(select(Subscription).where(Subscription.organization_id == org))
    if sub is None:
        raise NotFound("Subscription not found")
    state, grace_until = access_state(sub, datetime.now(UTC))
    return Entitlements(sub, PLANS[Plan(sub.plan)], state, grace_until)


async def usage(session: AsyncSession, org: uuid.UUID) -> dict[Resource, int]:
    async def count(model: type, *where: object) -> int:
        stmt = select(func.count()).select_from(model).where(model.organization_id == org, *where)  # type: ignore[attr-defined]
        return await session.scalar(stmt) or 0

    pending = await count(
        Invitation, Invitation.accepted_at.is_(None), Invitation.expires_at > datetime.now(UTC)
    )
    return {
        "sites": await count(Site),
        "devices": await count(Device),
        # Pending invitations hold a seat, otherwise the limit could be overshot.
        "users": await count(Membership) + pending,
    }


async def ensure_can_add(session: AsyncSession, org: uuid.UUID, resource: Resource) -> None:
    ent = await get_entitlements(session, org)
    if ent.state == "EXPIRED":
        raise PlanLimitExceeded("Subscription expired", code="subscription_expired")
    limit = {
        "sites": ent.spec.max_sites,
        "devices": ent.spec.max_devices,
        "users": ent.spec.max_users,
    }[resource]
    if limit is None:
        return
    current = (await usage(session, org))[resource]
    if current >= limit:
        raise PlanLimitExceeded(
            f"The {ent.spec.plan} plan allows {limit} {resource}",
            details={"resource": resource, "limit": limit, "current": current},
        )


async def ensure_feature(session: AsyncSession, org: uuid.UUID, feature: Feature) -> None:
    ent = await get_entitlements(session, org)
    if ent.state == "EXPIRED":
        raise PlanLimitExceeded("Subscription expired", code="subscription_expired")
    if feature not in ent.spec.features:
        raise PlanLimitExceeded(
            f"'{feature}' is not included in the {ent.spec.plan} plan",
            code="feature_not_in_plan",
            details={"feature": feature, "plan": ent.spec.plan},
        )


def new_trial(org: uuid.UUID, days: int) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        organization_id=org,
        plan=Plan.PROFESSIONAL,
        billing_cycle=BillingCycle.MONTHLY,
        status=SubscriptionStatus.TRIALING,
        current_period_start=now,
        current_period_end=now + timedelta(days=days),
    )


def period_length(cycle: BillingCycle) -> timedelta:
    return timedelta(days=365 if cycle == BillingCycle.ANNUAL else 30)


# --------------------------------------------------------------------------- payments


class PaymentProvider(Protocol):
    name: str

    async def charge(self, record: BillingRecord) -> str | None:
        """Collect payment for ``record``; return the provider reference, or
        ``None`` when settlement happens later (invoice stays PENDING)."""


class ManualPaymentProvider:
    """No online collection: an invoice is recorded and settled out of band
    (bank transfer, purchase order), then marked PAID by a platform admin."""

    name = "manual"

    async def charge(self, record: BillingRecord) -> str | None:
        return None


async def change_plan(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    plan: Plan,
    cycle: BillingCycle,
    amount: int,
    currency: str,
    provider: PaymentProvider,
) -> tuple[Subscription, BillingRecord]:
    """Start a new paid period and record its invoice."""
    sub = (await get_entitlements(session, org)).subscription
    now = datetime.now(UTC)
    sub.plan, sub.billing_cycle, sub.status = plan, cycle, SubscriptionStatus.ACTIVE
    sub.current_period_start, sub.current_period_end = now, now + period_length(cycle)
    record = BillingRecord(
        organization_id=org,
        plan=plan,
        billing_cycle=cycle,
        amount=amount,
        currency=currency,
        period_start=sub.current_period_start,
        period_end=sub.current_period_end,
        status=BillingStatus.PENDING,
        provider=provider.name,
    )
    reference = await provider.charge(record)
    if reference is not None:
        record.status, record.provider_reference = BillingStatus.PAID, reference
    session.add(record)
    await session.flush()
    return sub, record
