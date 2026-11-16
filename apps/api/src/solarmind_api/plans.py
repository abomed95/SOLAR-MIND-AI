"""SaaS plan catalogue: limits and feature entitlements.

Prices are intentionally absent: commercial pricing is a business decision
that has not been provided. ``None`` means "unlimited".
"""

from dataclasses import dataclass
from enum import StrEnum

from solarmind_api.enums import Plan


class Feature(StrEnum):
    AI_ASSISTANT = "ai_assistant"
    PDF_REPORTS = "pdf_reports"
    ANOMALY_DETECTION = "anomaly_detection"
    PREDICTIVE_MAINTENANCE = "predictive_maintenance"
    WHATSAPP_NOTIFICATIONS = "whatsapp_notifications"
    AUDIT_EXPORT = "audit_export"


@dataclass(frozen=True)
class PlanSpec:
    plan: Plan
    max_sites: int | None
    max_devices: int | None
    max_users: int | None
    features: frozenset[Feature]


_ALL = frozenset(Feature)

PLANS: dict[Plan, PlanSpec] = {
    Plan.ESSENTIAL: PlanSpec(Plan.ESSENTIAL, 2, 10, 5, frozenset({Feature.PDF_REPORTS})),
    Plan.PROFESSIONAL: PlanSpec(
        Plan.PROFESSIONAL,
        10,
        100,
        25,
        frozenset({Feature.PDF_REPORTS, Feature.AI_ASSISTANT, Feature.ANOMALY_DETECTION}),
    ),
    Plan.ENTERPRISE: PlanSpec(Plan.ENTERPRISE, None, None, None, _ALL),
    Plan.GOVERNMENT: PlanSpec(Plan.GOVERNMENT, None, None, None, _ALL),
    Plan.PARTNER: PlanSpec(Plan.PARTNER, 50, 500, 100, _ALL),
}
