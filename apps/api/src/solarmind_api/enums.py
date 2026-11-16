"""Business enumerations. Stored as text; validated at the API boundary."""

from enum import StrEnum


class Role(StrEnum):
    ORGANIZATION_ADMIN = "ORGANIZATION_ADMIN"
    ENERGY_MANAGER = "ENERGY_MANAGER"
    TECHNICAL_MANAGER = "TECHNICAL_MANAGER"
    TECHNICIAN = "TECHNICIAN"
    VIEWER = "VIEWER"
    GOVERNMENT_AUDITOR = "GOVERNMENT_AUDITOR"


class SiteKind(StrEnum):
    SOLAR_PLANT = "SOLAR_PLANT"
    COMMERCIAL = "COMMERCIAL"
    HOTEL = "HOTEL"
    INDUSTRIAL = "INDUSTRIAL"
    HOSPITAL = "HOSPITAL"
    PUBLIC_BUILDING = "PUBLIC_BUILDING"
    TELECOM = "TELECOM"
    OTHER = "OTHER"


class AssetKind(StrEnum):
    PV_ARRAY = "PV_ARRAY"
    INVERTER = "INVERTER"
    BATTERY = "BATTERY"
    METER = "METER"
    OTHER = "OTHER"


class DeviceKind(StrEnum):
    INVERTER = "INVERTER"
    BATTERY_BMS = "BATTERY_BMS"
    METER = "METER"
    WEATHER_STATION = "WEATHER_STATION"


class DeviceProtocol(StrEnum):
    MOCK = "MOCK"
    MODBUS_TCP = "MODBUS_TCP"
    MODBUS_RTU = "MODBUS_RTU"


class Plan(StrEnum):
    ESSENTIAL = "ESSENTIAL"
    PROFESSIONAL = "PROFESSIONAL"
    ENTERPRISE = "ENTERPRISE"
    GOVERNMENT = "GOVERNMENT"
    PARTNER = "PARTNER"


class BillingCycle(StrEnum):
    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"


class SubscriptionStatus(StrEnum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


class AlertCategory(StrEnum):
    TEMPERATURE = "TEMPERATURE"
    BATTERY = "BATTERY"
    PRODUCTION = "PRODUCTION"
    COMMUNICATION = "COMMUNICATION"
    GRID = "GRID"
    DATA_QUALITY = "DATA_QUALITY"
    OTHER = "OTHER"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class RuleOperator(StrEnum):
    GT = "GT"
    LT = "LT"


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class Channel(StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class BillingStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    VOID = "VOID"
