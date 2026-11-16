"""Agent 2 - Performance: production / consumption KPIs and comparisons.

Deterministic arithmetic on calculated daily energies. No model, no guess.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from solarmind_energy import performance_ratio, specific_yield

# A day is used for "best / worst day" only if most of it is backed by data.
MIN_DAY_COVERAGE = 0.2


@dataclass(frozen=True)
class DailyEnergy:
    day: date
    production_kwh: float | None
    consumption_kwh: float | None = None
    insolation_kwh_m2: float | None = None
    coverage: float = 0.0  # share of the 24 h backed by PV power samples


def _sum(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _change(current: float | None, previous: float | None) -> float | None:
    if current is None or not previous:
        return None
    return (current - previous) / previous


def analyze(
    days: Sequence[DailyEnergy], *, capacity_kwp: float,
    previous: Sequence[DailyEnergy] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Summarize a period; optionally compare with the preceding period."""
    production = _sum([d.production_kwh for d in days])
    consumption = _sum([d.consumption_kwh for d in days])
    insolation = _sum([d.insolation_kwh_m2 for d in days])
    producing = [d for d in days if d.production_kwh is not None]
    reliable = [d for d in producing if d.coverage >= MIN_DAY_COVERAGE]
    best = max(reliable, key=lambda d: d.production_kwh or 0, default=None)
    worst = min(reliable, key=lambda d: d.production_kwh or 0, default=None)

    out: dict[str, Any] = {
        "basis": "calculated",
        "days_in_period": len(days),
        "days_with_data": len(producing),
        "production_kwh": None if production is None else round(production, 2),
        "consumption_kwh": None if consumption is None else round(consumption, 2),
        "average_daily_production_kwh": (
            round(production / len(producing), 2) if production is not None and producing else None
        ),
        "specific_yield_kwh_per_kwp": (
            None if production is None else _round(specific_yield(production, capacity_kwp))
        ),
        "performance_ratio": (
            None
            if production is None
            else _round(performance_ratio(production, insolation or 0.0, capacity_kwp))
        ),  # fmt: skip
        "best_day": _day(best),
        "worst_day": _day(worst),
        "notes": [],
    }
    if len(producing) < len(days):
        out["notes"].append(
            f"{len(days) - len(producing)} day(s) have no production data and are not counted "
            "as zero."
        )
    if insolation is None and production is not None:
        out["notes"].append("No irradiance measurement: performance ratio is not available.")
    if previous is not None:
        prev_production = _sum([d.production_kwh for d in previous])
        change = _change(production, prev_production)
        out["previous_period"] = {
            "production_kwh": None if prev_production is None else round(prev_production, 2),
            "production_change": None if change is None else round(change, 4),
        }
    return out


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _day(day: DailyEnergy | None) -> dict[str, Any] | None:
    if day is None or day.production_kwh is None:
        return None
    return {"day": day.day.isoformat(), "production_kwh": round(day.production_kwh, 2)}


def compare_sites(sites: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank sites by specific yield (kWh/kWp), the size-independent measure.

    Each input: ``{"name", "capacity_kwp", "production_kwh", "insolation_kwh_m2"}``.
    Sites without production data are listed last, unranked.
    """
    rows = []
    for site in sites:
        production = site.get("production_kwh")
        capacity = site["capacity_kwp"]
        rows.append({
            "name": site["name"],
            "capacity_kwp": capacity,
            "production_kwh": None if production is None else round(production, 2),
            "specific_yield_kwh_per_kwp": (
                None if production is None else _round(specific_yield(production, capacity))
            ),
            "performance_ratio": (
                None if production is None
                else _round(performance_ratio(production, site.get("insolation_kwh_m2") or 0.0,
                                              capacity))
            ),
        })  # fmt: skip
    ranked = sorted(
        rows,
        key=lambda r: (r["specific_yield_kwh_per_kwp"] is None,
                       -(r["specific_yield_kwh_per_kwp"] or 0)),
    )  # fmt: skip
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position if row["specific_yield_kwh_per_kwp"] is not None else None
    return ranked
