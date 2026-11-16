"""Deterministic solar energy calculations (no I/O, no AI).

Conventions
-----------
* Power in kW, energy in kWh, irradiance in W/m2, insolation in kWh/m2.
* Timestamps are timezone-aware.
* Missing data is never treated as zero. An interval longer than ``max_gap_s``
  is a *gap*: it contributes no energy and is reported as uncovered time, so a
  caller can tell "produced nothing" from "we do not know".
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

STC_IRRADIANCE_KW_M2 = 1.0  # reference irradiance at Standard Test Conditions


@dataclass(frozen=True)
class Sample:
    time: datetime
    value: float


@dataclass(frozen=True)
class Integration:
    """Result of integrating a sampled quantity over time."""

    total: float  # kWh for power in kW; Wh/m2 for irradiance in W/m2
    covered_s: float  # time actually covered by valid consecutive samples
    gap_s: float  # time between samples that was too long to interpolate
    samples: int

    def coverage(self, window_s: float) -> float:
        """Share of ``window_s`` for which the integral is backed by data."""
        return min(1.0, self.covered_s / window_s) if window_s > 0 else 0.0


def integrate(samples: Iterable[Sample], *, max_gap_s: float) -> Integration:
    """Trapezoidal integration of irregularly sampled values, in value x hours.

    Uses the real interval between consecutive samples, so jitter and changes
    of sampling rate are handled. Duplicate timestamps contribute nothing.
    """
    if max_gap_s <= 0:
        raise ValueError("max_gap_s must be positive")
    ordered = sorted(samples, key=lambda s: s.time)
    total = covered = gap = 0.0
    for previous, current in zip(ordered, ordered[1:], strict=False):
        dt = (current.time - previous.time).total_seconds()
        if dt <= 0:
            continue
        if dt > max_gap_s:
            gap += dt
            continue
        total += (previous.value + current.value) / 2 * dt / 3600
        covered += dt
    return Integration(total=total, covered_s=covered, gap_s=gap, samples=len(ordered))


def availability(
    sample_times: Iterable[datetime], *, start: datetime, end: datetime, interval_s: float
) -> float | None:
    """Share of expected sampling slots in [start, end) holding at least one
    sample. ``None`` when the window holds no complete slot."""
    slots = int((end - start).total_seconds() // interval_s)
    if slots <= 0:
        return None
    seen = {
        int((t - start).total_seconds() // interval_s) for t in sample_times if start <= t < end
    }
    return len({s for s in seen if s < slots}) / slots


def performance_ratio(
    energy_ac_kwh: float, insolation_kwh_m2: float, capacity_kwp: float
) -> float | None:
    """Performance ratio per IEC 61724-1: final yield / reference yield.

        PR = (E_ac / P_stc) / (H_poa / G_stc)

    ``None`` when the reference data makes the ratio meaningless (no
    irradiance measurement, zero insolation, unknown capacity).
    """
    if capacity_kwp <= 0 or insolation_kwh_m2 <= 0:
        return None
    return (energy_ac_kwh / capacity_kwp) / (insolation_kwh_m2 / STC_IRRADIANCE_KW_M2)


def specific_yield(energy_ac_kwh: float, capacity_kwp: float) -> float | None:
    """Final yield in kWh per installed kWp."""
    return energy_ac_kwh / capacity_kwp if capacity_kwp > 0 else None


@dataclass(frozen=True)
class EnergyBalance:
    self_consumed_kwh: float  # PV energy used on site
    self_consumption_ratio: float | None  # share of production used on site
    self_sufficiency_ratio: float | None  # share of consumption covered without the grid


def energy_balance(
    *, production_kwh: float, consumption_kwh: float | None, grid_import_kwh: float | None,
    grid_export_kwh: float | None,
) -> EnergyBalance:  # fmt: skip
    """On-site use of PV energy. Without an export measurement all production
    is assumed consumed on site (stated by the caller as an assumption)."""
    export = grid_export_kwh or 0.0
    self_consumed = max(0.0, production_kwh - export)
    ratio = self_consumed / production_kwh if production_kwh > 0 else None
    sufficiency = None
    if consumption_kwh and consumption_kwh > 0 and grid_import_kwh is not None:
        sufficiency = min(1.0, max(0.0, (consumption_kwh - grid_import_kwh) / consumption_kwh))
    return EnergyBalance(self_consumed, ratio, sufficiency)


def estimated_savings(self_consumed_kwh: float, tariff_per_kwh: Decimal | None) -> Decimal | None:
    """Avoided grid purchases = self-consumed energy x grid tariff.

    An ESTIMATE: it assumes every self-consumed kWh would otherwise have been
    bought at ``tariff_per_kwh`` (flat tariff, no demand charges, no export
    revenue). Rounded to whole currency units.
    """
    if tariff_per_kwh is None:
        return None
    amount = Decimal(str(self_consumed_kwh)) * tariff_per_kwh
    return amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Trend:
    slope: float  # change of y per unit of x
    intercept: float
    r_squared: float
    points: int


def linear_trend(xs: Sequence[float], ys: Sequence[float]) -> Trend | None:
    """Ordinary least squares fit. ``None`` with fewer than 3 points or no
    spread in ``xs``."""
    n = len(xs)
    if n != len(ys):
        raise ValueError("xs and ys must have the same length")
    if n < 3:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return Trend(slope, intercept, r_squared, n)


__all__ = [
    "EnergyBalance", "Integration", "Sample", "Trend", "availability", "energy_balance",
    "estimated_savings", "integrate", "linear_trend", "performance_ratio", "specific_yield",
]  # fmt: skip
