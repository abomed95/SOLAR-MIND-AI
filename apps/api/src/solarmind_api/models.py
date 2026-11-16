"""SQLAlchemy models.

Conventions: UUID primary keys, TIMESTAMPTZ everywhere (UTC), and an
``organization_id`` on every tenant-owned table (enforced again by RLS, see
``database/migrations``). JSONB is reserved for genuinely free-form data.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        datetime: TIMESTAMP(timezone=True),
        uuid.UUID: PGUUID(as_uuid=True),
    }


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _org() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _fk(target: str, *, ondelete: str = "CASCADE", index: bool = True) -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey(target, ondelete=ondelete), index=index)


def _fk_null(target: str, *, index: bool = True) -> Mapped[uuid.UUID | None]:
    return mapped_column(ForeignKey(target, ondelete="SET NULL"), index=index, nullable=True)


# --------------------------------------------------------------------------- identity


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    country: Mapped[str] = mapped_column(String(2), default="DJ")
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Djibouti")
    locale: Mapped[str] = mapped_column(String(5), default="fr")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(String(5), default="fr")
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None]


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created()


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    organization_id: Mapped[uuid.UUID | None] = _fk_null("organizations.id")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID | None]
    details: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- billing


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    plan: Mapped[str] = mapped_column(String(20))
    billing_cycle: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12))
    current_period_start: Mapped[datetime]
    current_period_end: Mapped[datetime]
    grace_period_days: Mapped[int] = mapped_column(Integer, default=7, server_default=text("7"))
    created_at: Mapped[datetime] = _created()


class BillingRecord(Base):
    __tablename__ = "billing_records"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    plan: Mapped[str] = mapped_column(String(20))
    billing_cycle: Mapped[str] = mapped_column(String(10))
    # Whole currency units. DJF has no decimals; never store money as float.
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(30))
    provider_reference: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- assets


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Djibouti")
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    installed_capacity_kwp: Mapped[float] = mapped_column(Float)
    battery_capacity_kwh: Mapped[float | None] = mapped_column(Float)
    # Grid tariff used for *estimated* savings only.
    tariff_per_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    currency: Mapped[str] = mapped_column(String(3), default="DJF")
    expected_interval_s: Mapped[int] = mapped_column(Integer, default=300)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created()


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID] = _fk("sites.id")
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    manufacturer: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    installed_at: Mapped[datetime | None]
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created()


class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID] = _fk("sites.id")
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen_at: Mapped[datetime | None]
    # Number of records still waiting in the gateway's local buffer, as last reported.
    buffered_records: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID] = _fk("sites.id")
    asset_id: Mapped[uuid.UUID | None] = _fk_null("assets.id")
    gateway_id: Mapped[uuid.UUID | None] = _fk_null("gateways.id")
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    protocol: Mapped[str] = mapped_column(String(12))
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- telemetry


class Telemetry(Base):
    """Raw measurements. Append-only: the application role cannot UPDATE or DELETE.

    The primary key makes ingestion idempotent: replaying a batch inserts nothing.
    No foreign keys on purpose (hypertable write path); ownership is checked at
    ingestion and history survives device deletion.
    """

    __tablename__ = "telemetry"
    __table_args__ = (
        Index("ix_telemetry_org_time", "organization_id", "time"),
        Index("ix_telemetry_site_metric_time", "site_id", "metric_name", "time"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(40), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID]
    site_id: Mapped[uuid.UUID]
    value: Mapped[float] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(10))
    ingested_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- alerts


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(20))
    metric_name: Mapped[str] = mapped_column(String(40))
    operator: Mapped[str] = mapped_column(String(2))
    threshold: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = _created()


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_org_status", "organization_id", "status"),
        # Deduplication: at most one *active* alert per (device, key).
        Index(
            "uq_alerts_active_dedup",
            "organization_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("status <> 'RESOLVED'"),
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID] = _fk("sites.id")
    device_id: Mapped[uuid.UUID | None] = _fk_null("devices.id")
    rule_id: Mapped[uuid.UUID | None] = _fk_null("alert_rules.id", index=False)
    dedup_key: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(14))
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    # Measured values that triggered the alert (metric, value, threshold, time).
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    assigned_to: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    acknowledged_by: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    acknowledged_at: Mapped[datetime | None]
    resolved_by: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    resolved_at: Mapped[datetime | None]
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    alert_id: Mapped[uuid.UUID] = _fk("alerts.id")
    actor_id: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    event: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- notifications


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    alert_id: Mapped[uuid.UUID | None] = _fk_null("alerts.id", index=False)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10))
    read_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class NotificationDelivery(Base):
    """Outbox for e-mail / WhatsApp, retried by the worker."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (Index("ix_deliveries_pending", "status", "next_attempt_at"),)

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    channel: Mapped[str] = mapped_column(String(10))
    recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    whatsapp_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    whatsapp_number: Mapped[str | None] = mapped_column(String(32))
    min_severity: Mapped[str] = mapped_column(String(10), default="WARNING")


# --------------------------------------------------------------------------- maintenance


class MaintenanceTicket(Base):
    __tablename__ = "maintenance_tickets"
    __table_args__ = (Index("ix_tickets_org_status", "organization_id", "status"),)

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID] = _fk("sites.id")
    asset_id: Mapped[uuid.UUID | None] = _fk_null("assets.id", index=False)
    device_id: Mapped[uuid.UUID | None] = _fk_null("devices.id", index=False)
    alert_id: Mapped[uuid.UUID | None] = _fk_null("alerts.id", index=False)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12))
    assigned_to: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    created_by: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    scheduled_for: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class TicketNote(Base):
    __tablename__ = "ticket_notes"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    ticket_id: Mapped[uuid.UUID] = _fk("maintenance_tickets.id")
    author_id: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    # "NOTE" for free text, "STATUS" for an automatic status-change entry.
    kind: Mapped[str] = mapped_column(String(10), default="NOTE")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    ticket_id: Mapped[uuid.UUID] = _fk("maintenance_tickets.id")
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(300))
    uploaded_by: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- reports & AI


class EnergyReport(Base):
    __tablename__ = "energy_reports"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    site_id: Mapped[uuid.UUID] = _fk("sites.id")
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    storage_key: Mapped[str] = mapped_column(String(300))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Headline figures shown in the list; the PDF is the full document.
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    generated_by: Mapped[uuid.UUID | None] = _fk_null("users.id", index=False)
    created_at: Mapped[datetime] = _created()


class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    conversation_id: Mapped[uuid.UUID] = _fk("ai_conversations.id")
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    # Tools called to build the answer, with their arguments (never raw SQL).
    tool_calls: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default=text("'[]'"))
    provider: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = _created()


# Tables whose rows belong to exactly one organization (RLS: org match or bypass).
TENANT_TABLES = [
    "memberships", "invitations", "audit_logs", "subscriptions", "billing_records", "sites",
    "assets", "gateways", "devices", "telemetry", "alert_rules", "alerts", "alert_events",
    "notifications", "notification_deliveries", "notification_preferences",
    "maintenance_tickets", "ticket_notes", "attachments", "energy_reports",
    "ai_conversations", "ai_messages",
]  # fmt: skip
# Tables reachable only by system transactions (authentication internals).
SYSTEM_TABLES = ["sessions", "password_resets"]
