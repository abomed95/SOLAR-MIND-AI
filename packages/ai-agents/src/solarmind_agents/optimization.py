"""Agent 5 - Optimization: recommendations with explicit assumptions.

Recommendations are rule-based and read-only: nothing here changes equipment
settings. Savings figures are estimates and say what they assume.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from solarmind_agents.types import Finding
from solarmind_energy import estimated_savings

_SEVERITY_WEIGHT = {"CRITICAL": 100, "HIGH": 60, "WARNING": 25, "INFO": 5}


@dataclass(frozen=True)
class OptimizationInputs:
    period_days: int
    production_kwh: float | None
    consumption_kwh: float | None
    grid_import_kwh: float | None
    grid_export_kwh: float | None
    performance_ratio: float | None
    battery_capacity_kwh: float | None
    battery_soc_min: float | None
    battery_soc_max: float | None
    tariff_per_kwh: Decimal | None
    currency: str = "DJF"


def recommend(inputs: OptimizationInputs) -> list[Finding]:
    findings: list[Finding] = []
    production, export, imported = (
        inputs.production_kwh, inputs.grid_export_kwh, inputs.grid_import_kwh)  # fmt: skip
    period = f"over the last {inputs.period_days} days"

    # 1. Energy exported while the site also buys from the grid.
    if production and export and imported and export / production > 0.15 and imported > 0:
        recoverable = min(export, imported)
        saving = estimated_savings(recoverable, inputs.tariff_per_kwh)
        has_battery = bool(inputs.battery_capacity_kwh)
        findings.append(Finding(
            code="shift_or_store_surplus",
            severity="WARNING",
            title="Surplus solar energy is exported while the site still buys from the grid",
            detail=(
                f"{export:.0f} kWh were exported and {imported:.0f} kWh imported {period}. "
                + ("Shifting flexible loads to midday or reviewing battery dispatch"
                   if has_battery else
                   "Shifting flexible loads to midday, or adding storage,")
                + " could keep part of that energy on site."
            ),
            basis="estimated",
            evidence={"exported_kwh": round(export, 1), "imported_kwh": round(imported, 1),
                      "export_share": round(export / production, 3),
                      "recoverable_kwh_upper_bound": round(recoverable, 1),
                      "estimated_saving": None if saving is None else int(saving),
                      "currency": inputs.currency},
            assumptions=(
                "Upper bound: assumes every exported kWh could be used on site instead.",
                "Flat tariff equal to the site's configured tariff; export revenue ignored.",
            ),
        ))  # fmt: skip

    # 2. Battery that never works through its range.
    if inputs.battery_capacity_kwh and inputs.battery_soc_min is not None \
            and inputs.battery_soc_max is not None:  # fmt: skip
        swing = inputs.battery_soc_max - inputs.battery_soc_min
        if swing < 20:
            findings.append(Finding(
                code="battery_underused",
                severity="INFO",
                title="The battery barely cycles",
                detail=(
                    f"State of charge stayed between {inputs.battery_soc_min:.0f}% and "
                    f"{inputs.battery_soc_max:.0f}% {period}. Check the dispatch settings: "
                    "the storage may not be contributing to self-consumption."
                ),
                basis="calculated",
                evidence={"soc_min": inputs.battery_soc_min, "soc_max": inputs.battery_soc_max,
                          "swing_points": round(swing, 1)},
                assumptions=("A wider daily swing is normal for a battery used for "
                             "self-consumption; backup-only batteries legitimately stay full.",),
            ))  # fmt: skip
        if inputs.battery_soc_min < 15:
            findings.append(Finding(
                code="battery_deep_discharge",
                severity="WARNING",
                title="The battery is discharged very deeply",
                detail=(
                    f"State of charge dropped to {inputs.battery_soc_min:.0f}% {period}. "
                    "Repeated deep discharge shortens battery life for most chemistries."
                ),
                basis="measured",
                evidence={"soc_min": inputs.battery_soc_min},
                assumptions=("The acceptable depth of discharge depends on the battery "
                             "chemistry and the manufacturer's specification.",),
            ))  # fmt: skip

    # 3. Low performance ratio.
    if inputs.performance_ratio is not None and inputs.performance_ratio < 0.7:
        findings.append(Finding(
            code="low_performance_ratio",
            severity="WARNING",
            title="Performance ratio is below the usual range",
            detail=(
                f"Performance ratio was {inputs.performance_ratio:.2f} {period}. Inspect for "
                "soiling, shading, inverter clipping or string faults."
            ),
            basis="calculated",
            evidence={"performance_ratio": round(inputs.performance_ratio, 3)},
            assumptions=(
                "Well-performing plants typically reach 0.75 to 0.85; the achievable value "
                "depends on climate, design and sensor placement.",
            ),
        ))  # fmt: skip

    # 4. Almost everything is imported.
    if inputs.consumption_kwh and imported is not None and inputs.consumption_kwh > 0:
        share = imported / inputs.consumption_kwh
        if share > 0.7:
            findings.append(Finding(
                code="high_grid_dependence",
                severity="INFO",
                title="Most of the consumption still comes from the grid",
                detail=f"{share:.0%} of the energy consumed {period} was imported.",
                basis="calculated",
                evidence={"imported_kwh": round(imported, 1),
                          "consumption_kwh": round(inputs.consumption_kwh, 1),
                          "grid_share": round(share, 3)},
                assumptions=("Sizing additional PV or storage requires a dedicated study.",),
            ))  # fmt: skip
    return findings


def prioritize_maintenance(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order open alerts for intervention: severity first, then recurrence and age.

    Each alert: ``{"id", "title", "severity", "occurrence_count", "age_hours", ...}``.
    """

    def score(alert: dict[str, Any]) -> float:
        return float(
            _SEVERITY_WEIGHT.get(alert["severity"], 0)
            + min(20, alert.get("occurrence_count", 1))
            + min(20, alert.get("age_hours", 0) / 12)
        )

    ranked = sorted(alerts, key=score, reverse=True)
    return [{**a, "priority_score": round(score(a), 1), "priority_rank": i}
            for i, a in enumerate(ranked, start=1)]  # fmt: skip
