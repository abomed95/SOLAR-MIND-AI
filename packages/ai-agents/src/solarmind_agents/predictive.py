"""Agent 4 - Predictive Maintenance (rule-based).

This agent ranks inspection priority from observed history. It does NOT
predict a failure date or a remaining useful life: doing so would require
validated run-to-failure data for the installed equipment, which the platform
does not have. Its output is a risk level with the reasons behind it.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["INSUFFICIENT_DATA", "LOW", "MEDIUM", "HIGH"]
MIN_HISTORY_DAYS = 14

DISCLAIMER = (
    "Rule-based inspection priority derived from observed history. It is not a prediction "
    "of failure date or remaining useful life."
)


@dataclass(frozen=True)
class MaintenanceInputs:
    history_days: int  # days of telemetry available for the asset / site
    critical_alerts_30d: int = 0
    high_alerts_30d: int = 0
    temperature_alerts_30d: int = 0
    communication_losses_30d: int = 0
    # Relative drop of the performance indicator found by the degradation rule.
    degradation_drop: float | None = None
    availability_30d: float | None = None  # 0..1
    open_tickets: int = 0


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    score: int
    factors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    recommendation: str = ""
    disclaimer: str = DISCLAIMER

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "score": self.score, "factors": list(self.factors),
            "recommendation": self.recommendation, "basis": "estimated",
            "disclaimer": self.disclaimer,
        }  # fmt: skip


def assess_risk(inputs: MaintenanceInputs) -> RiskAssessment:
    if inputs.history_days < MIN_HISTORY_DAYS:
        return RiskAssessment(
            level="INSUFFICIENT_DATA",
            score=0,
            recommendation=(
                f"Only {inputs.history_days} day(s) of history: at least {MIN_HISTORY_DAYS} "
                "are needed before a maintenance risk can be estimated."
            ),
        )

    factors: list[dict[str, Any]] = []

    def add(points: int, reason: str, **evidence: Any) -> None:
        if points:
            factors.append({"points": points, "reason": reason, "evidence": evidence})

    add(min(40, 20 * inputs.critical_alerts_30d),
        "Critical alerts in the last 30 days", count=inputs.critical_alerts_30d)  # fmt: skip
    add(min(20, 5 * inputs.high_alerts_30d),
        "High-severity alerts in the last 30 days", count=inputs.high_alerts_30d)  # fmt: skip
    add(min(20, 7 * inputs.temperature_alerts_30d),
        "Repeated over-temperature events (thermal stress)",
        count=inputs.temperature_alerts_30d)  # fmt: skip
    add(min(15, 5 * inputs.communication_losses_30d),
        "Communication losses (possible power or wiring issue)",
        count=inputs.communication_losses_30d)  # fmt: skip
    if inputs.degradation_drop is not None:
        add(30 if inputs.degradation_drop >= 0.2 else 15,
            "Gradual loss of performance", relative_drop=inputs.degradation_drop)  # fmt: skip
    if inputs.availability_30d is not None and inputs.availability_30d < 0.95:
        add(15 if inputs.availability_30d < 0.85 else 8,
            "Reduced availability over 30 days", availability=inputs.availability_30d)  # fmt: skip

    score = min(100, sum(f["points"] for f in factors))
    level: RiskLevel = "HIGH" if score >= 50 else "MEDIUM" if score >= 20 else "LOW"
    recommendation = {
        "HIGH": "Schedule an on-site inspection promptly and review the listed factors first.",
        "MEDIUM": "Plan an inspection at the next maintenance visit.",
        "LOW": "No action beyond routine maintenance is indicated by the available data.",
    }[level]
    if inputs.open_tickets and level != "LOW":
        recommendation += f" {inputs.open_tickets} maintenance ticket(s) are already open."
    return RiskAssessment(level, score, tuple(factors), recommendation)
