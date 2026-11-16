"""Request and response models (the API contract)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import available_timezones

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
)

from solarmind_api.enums import (
    AlertCategory,
    AssetKind,
    BillingCycle,
    DeviceKind,
    DeviceProtocol,
    Plan,
    Role,
    RuleOperator,
    Severity,
    SiteKind,
    TicketPriority,
    TicketStatus,
)


def _timezone(value: str) -> str:
    if value not in available_timezones():
        raise ValueError("unknown IANA timezone")
    return value


def _lower(value: str) -> str:
    return value.lower()


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]
Email = Annotated[EmailStr, AfterValidator(_lower)]
Timezone = Annotated[str, AfterValidator(_timezone)]
Locale = Annotated[str, StringConstraints(pattern=r"^(fr|en)$")]
Token = Annotated[str, StringConstraints(min_length=20, max_length=200)]
TotpCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- auth


class RegisterIn(In):
    organization_name: Name
    full_name: Name
    email: Email
    password: Password


class LoginIn(In):
    email: Email
    password: Annotated[str, StringConstraints(max_length=128)]
    totp_code: TotpCode | None = None
    organization_id: uuid.UUID | None = None


class SwitchOrganizationIn(In):
    organization_id: uuid.UUID


class ForgotPasswordIn(In):
    email: Email


class ResetPasswordIn(In):
    token: Token
    new_password: Password


class ChangePasswordIn(In):
    current_password: Annotated[str, StringConstraints(max_length=128)]
    new_password: Password


class MfaCodeIn(In):
    code: TotpCode


class MfaDisableIn(In):
    password: Annotated[str, StringConstraints(max_length=128)]
    code: TotpCode


class AcceptInvitationIn(In):
    token: Token
    full_name: Name
    password: Password


class UserOut(Out):
    id: uuid.UUID
    email: str
    full_name: str
    is_super_admin: bool
    mfa_enabled: bool
    locale: str


class OrganizationOut(Out):
    id: uuid.UUID
    name: str
    slug: str
    country: str
    timezone: str
    locale: str
    is_active: bool
    is_demo: bool
    created_at: datetime


class MembershipSummary(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    role: Role


class MeOut(BaseModel):
    user: UserOut
    organization: OrganizationOut | None
    role: Role | None
    permissions: list[str]
    memberships: list[MembershipSummary]


class LoginOut(MeOut):
    token: str
    expires_at: datetime


class SessionOut(Out):
    id: uuid.UUID
    ip: str | None
    user_agent: str | None
    created_at: datetime
    expires_at: datetime
    current: bool = False


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


# --------------------------------------------------------------------------- organization & users


class OrganizationUpdate(In):
    name: Name | None = None
    timezone: Timezone | None = None
    locale: Locale | None = None


class OrganizationCreate(In):
    """Platform administrator creates an organization and invites its first admin."""

    name: Name
    admin_email: Email
    plan: Plan = Plan.PROFESSIONAL


class OrganizationAdminUpdate(In):
    is_active: bool


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: Role
    mfa_enabled: bool
    joined_at: datetime


class MemberUpdate(In):
    role: Role


class InvitationCreate(In):
    email: Email
    role: Role


class InvitationOut(Out):
    id: uuid.UUID
    email: str
    role: Role
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None


class InvitationCreated(InvitationOut):
    # Shown once to the inviter so onboarding works even without SMTP.
    invitation_url: str


class AuditLogOut(Out):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    details: dict[str, Any]
    ip: str | None
    created_at: datetime


# --------------------------------------------------------------------------- sites & equipment


class SiteBase(In):
    name: Name
    kind: SiteKind = SiteKind.COMMERCIAL
    timezone: Timezone = "Africa/Djibouti"
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    installed_capacity_kwp: Annotated[float, Field(gt=0, le=1_000_000)]
    battery_capacity_kwh: Annotated[float, Field(ge=0, le=10_000_000)] | None = None
    tariff_per_kwh: Annotated[Decimal, Field(ge=0, le=100_000)] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "DJF"
    expected_interval_s: Annotated[int, Field(ge=10, le=3600)] = 300


class SiteCreate(SiteBase):
    # Declares the site as a simulation. Settable at creation only: simulated
    # equipment can never later be presented as real.
    is_simulated: bool = False


class SiteUpdate(In):
    name: Name | None = None
    kind: SiteKind | None = None
    timezone: Timezone | None = None
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    installed_capacity_kwp: Annotated[float, Field(gt=0, le=1_000_000)] | None = None
    battery_capacity_kwh: Annotated[float, Field(ge=0, le=10_000_000)] | None = None
    tariff_per_kwh: Annotated[Decimal, Field(ge=0, le=100_000)] | None = None
    expected_interval_s: Annotated[int, Field(ge=10, le=3600)] | None = None


class SiteOut(Out):
    id: uuid.UUID
    name: str
    kind: SiteKind
    timezone: str
    latitude: float | None
    longitude: float | None
    installed_capacity_kwp: float
    battery_capacity_kwh: float | None
    tariff_per_kwh: Decimal | None
    currency: str
    expected_interval_s: int
    is_simulated: bool
    created_at: datetime


class AssetCreate(In):
    site_id: uuid.UUID
    name: Name
    kind: AssetKind
    manufacturer: ShortText | None = None
    model: ShortText | None = None
    serial_number: ShortText | None = None
    installed_at: AwareDatetime | None = None


class AssetUpdate(In):
    name: Name | None = None
    manufacturer: ShortText | None = None
    model: ShortText | None = None
    serial_number: ShortText | None = None
    installed_at: AwareDatetime | None = None


class AssetOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    kind: AssetKind
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    installed_at: datetime | None
    created_at: datetime


class DeviceCreate(In):
    site_id: uuid.UUID
    name: Name
    kind: DeviceKind
    protocol: DeviceProtocol = DeviceProtocol.MOCK
    asset_id: uuid.UUID | None = None
    gateway_id: uuid.UUID | None = None
    is_simulated: bool = False


class DeviceUpdate(In):
    name: Name | None = None
    asset_id: uuid.UUID | None = None
    gateway_id: uuid.UUID | None = None


class DeviceOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID
    asset_id: uuid.UUID | None
    gateway_id: uuid.UUID | None
    name: str
    kind: DeviceKind
    protocol: DeviceProtocol
    is_simulated: bool
    last_seen_at: datetime | None
    created_at: datetime
    # Derived from last_seen_at and the site's expected sampling interval.
    status: str = "UNKNOWN"


class GatewayCreate(In):
    site_id: uuid.UUID
    name: Name


class GatewayOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    last_seen_at: datetime | None
    buffered_records: int | None
    created_at: datetime
    status: str = "UNKNOWN"


class GatewayCreated(GatewayOut):
    # Returned once; only its hash is stored.
    api_key: str


# --------------------------------------------------------------------------- alerts


class AlertRuleCreate(In):
    name: Name
    site_id: uuid.UUID | None = None
    category: AlertCategory = AlertCategory.OTHER
    metric_name: Annotated[str, StringConstraints(max_length=40)]
    operator: RuleOperator
    threshold: float
    severity: Severity


class AlertRuleUpdate(In):
    name: Name | None = None
    threshold: float | None = None
    severity: Severity | None = None
    enabled: bool | None = None


class AlertRuleOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID | None
    name: str
    category: AlertCategory
    metric_name: str
    operator: RuleOperator
    threshold: float
    severity: Severity
    enabled: bool


class AlertOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID
    device_id: uuid.UUID | None
    rule_id: uuid.UUID | None
    category: AlertCategory
    severity: Severity
    status: str
    title: str
    message: str
    evidence: dict[str, Any]
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    assigned_to: uuid.UUID | None
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_note: str | None


class AlertEventOut(Out):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    event: str
    note: str | None
    created_at: datetime


class AlertResolveIn(In):
    note: LongText | None = None


class AssignIn(In):
    user_id: uuid.UUID | None


class NotificationOut(Out):
    id: uuid.UUID
    alert_id: uuid.UUID | None
    title: str
    body: str
    severity: Severity
    read_at: datetime | None
    created_at: datetime


class NotificationPreferenceIO(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    email_enabled: bool = True
    whatsapp_enabled: bool = False
    whatsapp_number: Annotated[str, StringConstraints(pattern=r"^\+\d{6,15}$")] | None = None
    min_severity: Severity = Severity.WARNING


# --------------------------------------------------------------------------- maintenance


class TicketCreate(In):
    site_id: uuid.UUID
    title: Name
    description: LongText = ""
    priority: TicketPriority = TicketPriority.MEDIUM
    asset_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    alert_id: uuid.UUID | None = None
    assigned_to: uuid.UUID | None = None
    scheduled_for: AwareDatetime | None = None


class TicketUpdate(In):
    title: Name | None = None
    description: LongText | None = None
    priority: TicketPriority | None = None
    status: TicketStatus | None = None
    assigned_to: uuid.UUID | None = None
    scheduled_for: AwareDatetime | None = None


class TicketOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID
    asset_id: uuid.UUID | None
    device_id: uuid.UUID | None
    alert_id: uuid.UUID | None
    title: str
    description: str
    priority: TicketPriority
    status: TicketStatus
    assigned_to: uuid.UUID | None
    created_by: uuid.UUID | None
    scheduled_for: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime


class TicketNoteIn(In):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class TicketNoteOut(Out):
    id: uuid.UUID
    author_id: uuid.UUID | None
    kind: str
    body: str
    created_at: datetime


class AttachmentOut(Out):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


# --------------------------------------------------------------------------- reports, AI, billing


class ReportCreate(In):
    site_id: uuid.UUID
    period_start: AwareDatetime
    period_end: AwareDatetime


class ReportOut(Out):
    id: uuid.UUID
    site_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    size_bytes: int
    summary: dict[str, Any]
    created_at: datetime


class AskIn(In):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    conversation_id: uuid.UUID | None = None


class AIMessageOut(Out):
    id: uuid.UUID
    role: str
    content: str
    tool_calls: list[Any]
    provider: str | None
    created_at: datetime


class AIConversationOut(Out):
    id: uuid.UUID
    title: str
    created_at: datetime


class AskOut(BaseModel):
    conversation_id: uuid.UUID
    message: AIMessageOut


class PlanChangeIn(In):
    plan: Plan
    billing_cycle: BillingCycle
    # Agreed price in whole currency units. Pricing is contractual, not hard-coded.
    amount: Annotated[int, Field(ge=0, le=10**12)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "DJF"


class BillingRecordOut(Out):
    id: uuid.UUID
    plan: Plan
    billing_cycle: BillingCycle
    amount: int
    currency: str
    period_start: datetime
    period_end: datetime
    status: str
    provider: str
    provider_reference: str | None
    created_at: datetime
