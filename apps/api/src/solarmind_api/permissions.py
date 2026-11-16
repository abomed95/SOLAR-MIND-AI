"""Role-based access control: a static role -> permissions map."""

from enum import StrEnum

from solarmind_api.enums import Role


class P(StrEnum):
    ORG_MANAGE = "org:manage"
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    SITES_READ = "sites:read"
    SITES_WRITE = "sites:write"
    DEVICES_WRITE = "devices:write"
    TELEMETRY_READ = "telemetry:read"
    ALERTS_READ = "alerts:read"
    ALERTS_ACK = "alerts:ack"
    ALERTS_MANAGE = "alerts:manage"
    MAINTENANCE_READ = "maintenance:read"
    MAINTENANCE_WRITE = "maintenance:write"
    REPORTS_READ = "reports:read"
    REPORTS_GENERATE = "reports:generate"
    AI_USE = "ai:use"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    AUDIT_READ = "audit:read"


_READ_ONLY = frozenset(
    {P.SITES_READ, P.TELEMETRY_READ, P.ALERTS_READ, P.MAINTENANCE_READ, P.REPORTS_READ}
)

ROLE_PERMISSIONS: dict[Role, frozenset[P]] = {
    Role.ORGANIZATION_ADMIN: frozenset(P),
    Role.ENERGY_MANAGER: _READ_ONLY
    | {
        P.USERS_READ,
        P.SITES_WRITE,
        P.DEVICES_WRITE,
        P.ALERTS_ACK,
        P.ALERTS_MANAGE,
        P.MAINTENANCE_WRITE,
        P.REPORTS_GENERATE,
        P.AI_USE,
        P.BILLING_READ,
    },
    Role.TECHNICAL_MANAGER: _READ_ONLY
    | {
        P.USERS_READ,
        P.DEVICES_WRITE,
        P.ALERTS_ACK,
        P.ALERTS_MANAGE,
        P.MAINTENANCE_WRITE,
        P.REPORTS_GENERATE,
        P.AI_USE,
    },
    Role.TECHNICIAN: _READ_ONLY | {P.ALERTS_ACK, P.MAINTENANCE_WRITE, P.AI_USE},
    Role.VIEWER: _READ_ONLY,
    Role.GOVERNMENT_AUDITOR: _READ_ONLY | {P.AUDIT_READ, P.USERS_READ},
}


def permissions_for(role: Role) -> frozenset[P]:
    return ROLE_PERMISSIONS[role]
