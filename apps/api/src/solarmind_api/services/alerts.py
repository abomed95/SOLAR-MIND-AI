"""Alert rules, threshold evaluation, deduplication, lifecycle and notifications.

Every alert carries its evidence: the measured value, the threshold, the
instant of measurement and whether the data was MEASURED or SIMULATED.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.enums import (
    AlertCategory,
    AlertStatus,
    Channel,
    DeliveryStatus,
    Role,
    RuleOperator,
    Severity,
)
from solarmind_api.errors import Conflict, NotFound
from solarmind_api.models import (
    Alert,
    AlertEvent,
    AlertRule,
    Device,
    Membership,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    Site,
    User,
)
from solarmind_api.permissions import P, permissions_for
from solarmind_agents.fault_detection import ThresholdRule, evaluate_thresholds
from solarmind_schemas import METRICS

# Generic starting points, editable per organization. They are deliberately
# conservative and are NOT manufacturer limits: the right thresholds depend on
# the equipment datasheets.
DEFAULT_RULES: list[tuple[str, AlertCategory, str, RuleOperator, float, Severity]] = [
    ("Inverter temperature high", AlertCategory.TEMPERATURE, "inverter_temperature_c",
     RuleOperator.GT, 75.0, Severity.HIGH),
    ("Inverter temperature critical", AlertCategory.TEMPERATURE, "inverter_temperature_c",
     RuleOperator.GT, 85.0, Severity.CRITICAL),
    ("Battery state of charge low", AlertCategory.BATTERY, "battery_soc_percent",
     RuleOperator.LT, 15.0, Severity.WARNING),
    ("Battery temperature high", AlertCategory.BATTERY, "battery_temperature_c",
     RuleOperator.GT, 50.0, Severity.HIGH),
]  # fmt: skip

OFFLINE_AFTER_INTERVALS = 3


def add_default_rules(session: AsyncSession, organization_id: uuid.UUID) -> None:
    for name, category, metric, operator, threshold, severity in DEFAULT_RULES:
        session.add(
            AlertRule(
                organization_id=organization_id,
                name=name,
                category=category,
                metric_name=metric,
                operator=operator,
                threshold=threshold,
                severity=severity,
            )
        )


def _event(
    alert: Alert, event: str, actor: uuid.UUID | None, note: str | None = None
) -> AlertEvent:
    return AlertEvent(
        organization_id=alert.organization_id, alert_id=alert.id, actor_id=actor, event=event,
        note=note,
    )  # fmt: skip


# --------------------------------------------------------------------------- notifications


async def _notify(s: AsyncSession, alert: Alert) -> None:
    """In-app notification for every member who can read alerts, plus e-mail /
    WhatsApp deliveries (outbox rows sent and retried by the worker)."""
    members = await s.execute(
        select(User, Membership.role, NotificationPreference)
        .join(Membership, Membership.user_id == User.id)
        .outerjoin(
            NotificationPreference,
            (NotificationPreference.user_id == User.id)
            & (NotificationPreference.organization_id == alert.organization_id),
        )
        .where(Membership.organization_id == alert.organization_id, User.is_active.is_(True))
    )
    severity = Severity(alert.severity)
    for user, role, pref in members:
        if P.ALERTS_READ not in permissions_for(Role(role)):
            continue
        s.add(
            Notification(
                organization_id=alert.organization_id,
                user_id=user.id,
                alert_id=alert.id,
                title=alert.title,
                body=alert.message,
                severity=alert.severity,
            )  # fmt: skip
        )
        minimum = Severity(pref.min_severity) if pref else Severity.WARNING
        if severity.rank < minimum.rank:
            continue
        subject = f"[SOLAR MIND AI] {alert.severity} - {alert.title}"
        if pref is None or pref.email_enabled:
            s.add(
                NotificationDelivery(
                    organization_id=alert.organization_id,
                    user_id=user.id,
                    channel=Channel.EMAIL,
                    recipient=user.email,
                    subject=subject,
                    body=alert.message,
                    status=DeliveryStatus.PENDING,
                )  # fmt: skip
            )
        if pref is not None and pref.whatsapp_enabled and pref.whatsapp_number:
            s.add(
                NotificationDelivery(
                    organization_id=alert.organization_id,
                    user_id=user.id,
                    channel=Channel.WHATSAPP,
                    recipient=pref.whatsapp_number,
                    subject=subject,
                    body=alert.message,
                    status=DeliveryStatus.PENDING,
                )  # fmt: skip
            )


# --------------------------------------------------------------------------- raising


async def raise_alert(
    s: AsyncSession, *, organization_id: uuid.UUID, site_id: uuid.UUID,
    device_id: uuid.UUID | None, rule_id: uuid.UUID | None, dedup_key: str,
    category: AlertCategory, severity: Severity, title: str, message: str,
    evidence: dict[str, Any], seen_at: datetime, occurrences: int = 1,
) -> tuple[Alert, bool]:  # fmt: skip
    """Create an alert, or fold the event into the active alert with the same
    ``dedup_key``. Returns (alert, created)."""
    active = select(Alert).where(
        Alert.organization_id == organization_id,
        Alert.dedup_key == dedup_key,
        Alert.status != AlertStatus.RESOLVED,
    )
    existing = await s.scalar(active.with_for_update())
    if existing is None:
        alert = Alert(
            organization_id=organization_id, site_id=site_id, device_id=device_id, rule_id=rule_id,
            dedup_key=dedup_key, category=category, severity=severity, status=AlertStatus.OPEN,
            title=title, message=message, evidence=evidence, occurrence_count=occurrences,
            first_seen_at=seen_at, last_seen_at=seen_at,
        )  # fmt: skip
        try:
            # Savepoint: a concurrent ingestion may create the same alert first
            # (the partial unique index decides the winner).
            async with s.begin_nested():
                s.add(alert)
                await s.flush()
        except IntegrityError:
            existing = await s.scalar(active.with_for_update())
            if existing is None:
                raise
        else:
            s.add(_event(alert, "CREATED", None))
            await _notify(s, alert)
            return alert, True
    existing.occurrence_count += occurrences
    if seen_at >= existing.last_seen_at:
        existing.last_seen_at, existing.evidence, existing.message = seen_at, evidence, message
    existing.first_seen_at = min(existing.first_seen_at, seen_at)
    return existing, False


async def evaluate_rows(
    s: AsyncSession, organization_id: uuid.UUID, rows: list[dict[str, Any]]
) -> list[Alert]:
    """Apply the organization's threshold rules (agent 3) to freshly stored
    telemetry and raise or refresh the corresponding alerts."""
    if not rows:
        return []
    rules = [
        ThresholdRule(r.id, r.name, r.metric_name, r.operator, r.threshold, r.severity,
                      r.category, r.site_id)  # fmt: skip
        for r in await s.scalars(
            select(AlertRule).where(
                AlertRule.organization_id == organization_id, AlertRule.enabled.is_(True)
            )
        )
    ]
    raised = []
    for hit in evaluate_thresholds(rows, rules):
        rule, row = hit.rule, hit.row
        unit = METRICS[rule.metric].unit
        symbol = ">" if rule.operator == RuleOperator.GT else "<"
        alert, _ = await raise_alert(
            s, organization_id=organization_id, site_id=row["site_id"], device_id=hit.device_id,
            rule_id=rule.id, dedup_key=f"rule:{rule.id}:device:{hit.device_id}",
            category=AlertCategory(rule.category), severity=Severity(rule.severity),
            title=rule.name,
            message=(
                f"{rule.metric} = {row['value']:.2f} {unit} "
                f"(threshold {symbol} {rule.threshold:g} {unit}) "
                f"at {row['time'].isoformat()}"
            ),
            evidence={
                "kind": "threshold", "metric": rule.metric, "value": row["value"],
                "unit": unit, "operator": rule.operator, "threshold": rule.threshold,
                "measured_at": row["time"].isoformat(), "source": row["source"],
                "violations_in_batch": hit.count,
            },
            seen_at=row["time"], occurrences=hit.count,
        )  # fmt: skip
        raised.append(alert)
    return raised


async def detect_offline_devices(s: AsyncSession, now: datetime | None = None) -> tuple[int, int]:
    """Raise COMMUNICATION alerts for silent devices and auto-resolve those of
    devices that report again. ``s`` is a system transaction (all tenants).
    Returns (raised, resolved)."""
    now = now or datetime.now(UTC)
    raised = resolved = 0
    rows = await s.execute(
        select(Device, Site.expected_interval_s)
        .join(Site, Site.id == Device.site_id)
        .where(Device.last_seen_at.is_not(None))
    )
    for device, interval_s in rows:
        limit = timedelta(seconds=interval_s * OFFLINE_AFTER_INTERVALS)
        key = f"offline:device:{device.id}"
        if now - device.last_seen_at > limit:
            _, created = await raise_alert(
                s, organization_id=device.organization_id, site_id=device.site_id,
                device_id=device.id, rule_id=None, dedup_key=key,
                category=AlertCategory.COMMUNICATION, severity=Severity.HIGH,
                title=f"No data from {device.name}",
                message=(
                    f"Last measurement received at {device.last_seen_at.isoformat()}; expected "
                    f"every {interval_s} s. Production during the outage is unknown, not zero."
                ),
                evidence={"kind": "communication_loss",
                          "last_seen_at": device.last_seen_at.isoformat(),
                          "expected_interval_s": interval_s, "checked_at": now.isoformat()},
                seen_at=now, occurrences=0,
            )  # fmt: skip
            raised += created
        else:
            active = await s.scalar(
                select(Alert).where(
                    Alert.organization_id == device.organization_id,
                    Alert.dedup_key == key,
                    Alert.status != AlertStatus.RESOLVED,
                )
            )
            if active is not None:
                active.status, active.resolved_at = AlertStatus.RESOLVED, now
                active.resolution_note = "Device is reporting again (resolved automatically)."
                s.add(_event(active, "AUTO_RESOLVED", None))
                resolved += 1
    return raised, resolved


# --------------------------------------------------------------------------- lifecycle


async def acknowledge(s: AsyncSession, alert: Alert, actor: uuid.UUID) -> Alert:
    if alert.status != AlertStatus.OPEN:
        raise Conflict(f"Alert is already {alert.status}", code="invalid_transition")
    alert.status = AlertStatus.ACKNOWLEDGED
    alert.acknowledged_by, alert.acknowledged_at = actor, datetime.now(UTC)
    s.add(_event(alert, "ACKNOWLEDGED", actor))
    return alert


async def resolve(s: AsyncSession, alert: Alert, actor: uuid.UUID, note: str | None) -> Alert:
    if alert.status == AlertStatus.RESOLVED:
        raise Conflict("Alert is already RESOLVED", code="invalid_transition")
    alert.status = AlertStatus.RESOLVED
    alert.resolved_by, alert.resolved_at, alert.resolution_note = actor, datetime.now(UTC), note
    s.add(_event(alert, "RESOLVED", actor, note))
    return alert


async def assign(
    s: AsyncSession, alert: Alert, actor: uuid.UUID, assignee: uuid.UUID | None
) -> Alert:
    if assignee is not None:
        member = await s.scalar(
            select(Membership.id).where(
                Membership.organization_id == alert.organization_id, Membership.user_id == assignee
            )
        )
        if member is None:
            raise NotFound("Assignee is not a member of this organization")
    alert.assigned_to = assignee
    s.add(_event(alert, "ASSIGNED" if assignee else "UNASSIGNED", actor, str(assignee or "")))
    return alert
