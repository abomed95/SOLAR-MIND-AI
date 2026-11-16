"""Alert rules and alert lifecycle."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.enums import AlertCategory, RuleOperator, Severity
from solarmind_api.models import AlertRule

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


def add_default_rules(session: AsyncSession, organization_id: uuid.UUID) -> None:
    for name, category, metric, operator, threshold, severity in DEFAULT_RULES:
        session.add(
            AlertRule(
                organization_id=organization_id, name=name, category=category,
                metric_name=metric, operator=operator, threshold=threshold, severity=severity,
            )  # fmt: skip
        )
