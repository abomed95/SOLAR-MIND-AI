"""Alerts, alert rules, in-app notifications and notification preferences."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select, update

from solarmind_api import schemas as sc
from solarmind_api.deps import (
    Auth,
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
from solarmind_api.enums import AlertCategory, AlertStatus, Severity
from solarmind_api.errors import AppError
from solarmind_api.models import (
    Alert,
    AlertEvent,
    AlertRule,
    Notification,
    NotificationPreference,
    Site,
)
from solarmind_api.permissions import P
from solarmind_api.services import alerts as svc
from solarmind_api.services.audit import audit
from solarmind_schemas import METRICS

router = APIRouter(prefix="/alerts", tags=["alerts"])
notifications = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[sc.AlertOut])
async def list_alerts(
    s: TenantDB, params: Paging, auth: AuthContext = Perm(P.ALERTS_READ),
    status_: Annotated[AlertStatus | None, Query(alias="status")] = None, severity: Severity | None = None,
    category: AlertCategory | None = None, site_id: uuid.UUID | None = None,
    active: bool | None = None,
) -> Any:  # fmt: skip
    stmt = select(Alert).where(Alert.organization_id == auth.org)
    if status_:
        stmt = stmt.where(Alert.status == status_)
    if active is not None:
        condition = Alert.status != AlertStatus.RESOLVED
        stmt = stmt.where(condition if active else ~condition)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if category:
        stmt = stmt.where(Alert.category == category)
    if site_id:
        stmt = stmt.where(Alert.site_id == site_id)
    sortable = {"last_seen_at": Alert.last_seen_at, "first_seen_at": Alert.first_seen_at,
                "severity": Alert.severity, "status": Alert.status}  # fmt: skip
    items, total = await paginate(s, stmt, params, sortable, "-last_seen_at")
    return page(items, total, params)


# Declared before "/{alert_id}" so that "rules" is not parsed as an id.
@router.get("/rules", response_model=list[sc.AlertRuleOut])
async def list_rules(s: TenantDB, auth: AuthContext = Perm(P.ALERTS_READ)) -> Any:
    rows = await s.scalars(
        select(AlertRule).where(AlertRule.organization_id == auth.org).order_by(AlertRule.name)
    )
    return rows.all()


@router.post("/rules", response_model=sc.AlertRuleOut, status_code=201)
async def create_rule(
    data: sc.AlertRuleCreate, request: Request, s: TenantDB,
    auth: AuthContext = Perm(P.ALERTS_MANAGE),
) -> Any:  # fmt: skip
    if data.metric_name not in METRICS:
        raise AppError(f"Unknown metric '{data.metric_name}'", code="unknown_metric",
                       status_code=422)  # fmt: skip
    if data.site_id:
        await get_owned(s, Site, data.site_id, auth.org)
    rule = AlertRule(organization_id=auth.org, **data.model_dump())
    s.add(rule)
    await s.flush()
    audit(s, action="alert_rule.create", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="alert_rule", entity_id=rule.id, details=data.model_dump(mode="json"),
          ip=client_ip(request))  # fmt: skip
    return rule


@router.patch("/rules/{rule_id}", response_model=sc.AlertRuleOut)
async def update_rule(
    rule_id: uuid.UUID, data: sc.AlertRuleUpdate, request: Request, s: TenantDB,
    auth: AuthContext = Perm(P.ALERTS_MANAGE),
) -> Any:  # fmt: skip
    rule = await get_owned(s, AlertRule, rule_id, auth.org)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(rule, key, value)
    audit(s, action="alert_rule.update", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="alert_rule", entity_id=rule.id,
          details=data.model_dump(mode="json", exclude_unset=True), ip=client_ip(request))  # fmt: skip
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID, request: Request, s: TenantDB, auth: AuthContext = Perm(P.ALERTS_MANAGE)
) -> None:
    await s.delete(await get_owned(s, AlertRule, rule_id, auth.org))
    audit(s, action="alert_rule.delete", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="alert_rule", entity_id=rule_id, ip=client_ip(request))  # fmt: skip


@router.get("/{alert_id}", response_model=sc.AlertOut)
async def get_alert(
    alert_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.ALERTS_READ)
) -> Any:
    return await get_owned(s, Alert, alert_id, auth.org)


@router.get("/{alert_id}/events", response_model=list[sc.AlertEventOut])
async def alert_history(
    alert_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.ALERTS_READ)
) -> Any:
    await get_owned(s, Alert, alert_id, auth.org)
    rows = await s.scalars(
        select(AlertEvent)
        .where(AlertEvent.organization_id == auth.org, AlertEvent.alert_id == alert_id)
        .order_by(AlertEvent.created_at, AlertEvent.id)
    )
    return rows.all()


@router.post("/{alert_id}/acknowledge", response_model=sc.AlertOut)
async def acknowledge(
    alert_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.ALERTS_ACK)
) -> Any:
    alert = await get_owned(s, Alert, alert_id, auth.org)
    return await svc.acknowledge(s, alert, auth.user_id)


@router.post("/{alert_id}/resolve", response_model=sc.AlertOut)
async def resolve(
    alert_id: uuid.UUID, data: sc.AlertResolveIn, s: TenantDB,
    auth: AuthContext = Perm(P.ALERTS_ACK),
) -> Any:  # fmt: skip
    alert = await get_owned(s, Alert, alert_id, auth.org)
    return await svc.resolve(s, alert, auth.user_id, data.note)


@router.post("/{alert_id}/assign", response_model=sc.AlertOut)
async def assign(
    alert_id: uuid.UUID, data: sc.AssignIn, s: TenantDB,
    auth: AuthContext = Perm(P.ALERTS_MANAGE),
) -> Any:  # fmt: skip
    alert = await get_owned(s, Alert, alert_id, auth.org)
    return await svc.assign(s, alert, auth.user_id, data.user_id)


# --------------------------------------------------------------------------- notifications


@notifications.get("", response_model=Page[sc.NotificationOut])
async def list_notifications(
    s: TenantDB, params: Paging, auth: AuthContext = Perm(), unread: bool | None = None
) -> Any:
    stmt = select(Notification).where(
        Notification.organization_id == auth.org, Notification.user_id == auth.user_id
    )
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    items, total = await paginate(
        s, stmt, params, {"created_at": Notification.created_at}, "-created_at"
    )
    return page(items, total, params)


@notifications.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(s: TenantDB, auth: AuthContext = Perm()) -> None:
    await s.execute(
        update(Notification)
        .where(
            Notification.organization_id == auth.org,
            Notification.user_id == auth.user_id,
            Notification.read_at.is_(None),
        )  # fmt: skip
        .values(read_at=datetime.now(UTC))
    )


async def _preference(s: TenantDB, auth: Auth) -> NotificationPreference | None:
    return await s.scalar(  # type: ignore[no-any-return]
        select(NotificationPreference).where(
            NotificationPreference.organization_id == auth.org,
            NotificationPreference.user_id == auth.user_id,
        )
    )


@notifications.get("/preferences", response_model=sc.NotificationPreferenceIO)
async def get_preferences(s: TenantDB, auth: AuthContext = Perm()) -> Any:
    return await _preference(s, auth) or sc.NotificationPreferenceIO()


@notifications.put("/preferences", response_model=sc.NotificationPreferenceIO)
async def set_preferences(
    data: sc.NotificationPreferenceIO, s: TenantDB, auth: AuthContext = Perm()
) -> Any:
    if data.whatsapp_enabled and not data.whatsapp_number:
        raise AppError("A WhatsApp number is required", code="validation_error", status_code=422)
    preference = await _preference(s, auth)
    if preference is None:
        preference = NotificationPreference(organization_id=auth.org, user_id=auth.user_id)
        s.add(preference)
    for key, value in data.model_dump().items():
        setattr(preference, key, value)
    await s.flush()
    return preference
