"""Formula tests with hand-computed fixtures."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from solarmind_energy import (
    Sample,
    availability,
    energy_balance,
    estimated_savings,
    integrate,
    linear_trend,
    performance_ratio,
    specific_yield,
)

T0 = datetime(2026, 3, 10, 6, 0, tzinfo=UTC)


def series(*points: tuple[float, float]) -> list[Sample]:
    """(minutes after T0, value)"""
    return [Sample(T0 + timedelta(minutes=m), v) for m, v in points]


def test_constant_power_for_one_hour() -> None:
    result = integrate(series(*[(m, 10.0) for m in range(0, 61, 5)]), max_gap_s=900)
    assert result.total == pytest.approx(10.0)  # 10 kW x 1 h
    assert (result.covered_s, result.gap_s, result.samples) == (3600, 0, 13)
    assert result.coverage(3600) == 1.0


def test_linear_ramp_is_integrated_exactly() -> None:
    # 0 -> 60 kW over one hour: area of a triangle = 30 kWh.
    assert integrate(series((0, 0), (30, 30), (60, 60)), max_gap_s=3600).total == pytest.approx(30)


def test_irregular_sampling_uses_real_intervals() -> None:
    # 12 kW for 10 min, then 6 kW for 50 min -> 2 + 5 = 7 kWh,
    # with step changes approximated by trapezoids on short edges.
    result = integrate(series((0, 12), (10, 12), (10.001, 6), (60, 6)), max_gap_s=3600)
    assert result.total == pytest.approx(7.0, abs=0.001)


def test_a_gap_is_not_interpolated_and_not_counted_as_zero() -> None:
    # 10 kW, a 2 h hole in the data, then 10 kW again.
    samples = series((0, 10), (5, 10), (10, 10), (130, 10), (135, 10))
    result = integrate(samples, max_gap_s=900)
    assert result.total == pytest.approx(10 * 15 / 60)  # only the 15 covered minutes
    assert result.covered_s == 900 and result.gap_s == 7200
    assert result.coverage(135 * 60) == pytest.approx(900 / 8100)
    # With a tolerance that swallows the hole, the same data yields far more:
    # this is exactly the error the gap rule prevents.
    assert integrate(samples, max_gap_s=10_000).total == pytest.approx(22.5)


def test_integration_edge_cases() -> None:
    assert integrate([], max_gap_s=900).total == 0
    single = integrate(series((0, 50)), max_gap_s=900)
    assert (single.total, single.covered_s) == (0, 0)  # one sample covers no time
    # Unordered input and duplicate timestamps do not change the result.
    shuffled = integrate(series((10, 10), (0, 10), (5, 10), (5, 10)), max_gap_s=900)
    assert shuffled.total == pytest.approx(10 * 10 / 60)
    with pytest.raises(ValueError, match="positive"):
        integrate([], max_gap_s=0)


def test_integrating_irradiance_gives_insolation() -> None:
    # 1000 W/m2 for 5 h -> 5000 Wh/m2 = 5 kWh/m2 (5 peak sun hours).
    result = integrate(series(*[(m, 1000.0) for m in range(0, 301, 5)]), max_gap_s=900)
    assert result.total / 1000 == pytest.approx(5.0)


def test_availability_counts_slots_with_data() -> None:
    start, end = T0, T0 + timedelta(hours=1)
    full = [T0 + timedelta(minutes=m) for m in range(0, 60, 5)]
    assert availability(full, start=start, end=end, interval_s=300) == 1.0
    assert availability(full[:6], start=start, end=end, interval_s=300) == 0.5
    # Two samples in one slot count once; samples outside the window are ignored.
    noisy = [T0, T0 + timedelta(seconds=30), T0 - timedelta(minutes=5), end]
    assert availability(noisy, start=start, end=end, interval_s=300) == pytest.approx(1 / 12)
    assert availability([], start=start, end=end, interval_s=300) == 0.0
    assert availability(full, start=start, end=start, interval_s=300) is None


def test_performance_ratio_known_values() -> None:
    # 100 kWp, 5 kWh/m2 -> reference yield 5 h -> ideal 500 kWh. 400 kWh -> PR 0.80.
    assert performance_ratio(400, 5, 100) == pytest.approx(0.80)
    assert performance_ratio(500, 5, 100) == pytest.approx(1.0)
    # No usable reference -> no ratio, rather than a misleading number.
    assert performance_ratio(400, 0, 100) is None
    assert performance_ratio(400, 5, 0) is None


def test_specific_yield() -> None:
    assert specific_yield(450, 100) == pytest.approx(4.5)
    assert specific_yield(450, 0) is None


def test_energy_balance() -> None:
    balance = energy_balance(production_kwh=100, consumption_kwh=150, grid_import_kwh=70,
                             grid_export_kwh=20)  # fmt: skip
    assert balance.self_consumed_kwh == 80
    assert balance.self_consumption_ratio == pytest.approx(0.8)
    assert balance.self_sufficiency_ratio == pytest.approx(80 / 150)

    no_meter = energy_balance(production_kwh=100, consumption_kwh=None, grid_import_kwh=None,
                              grid_export_kwh=None)  # fmt: skip
    assert no_meter.self_consumed_kwh == 100 and no_meter.self_sufficiency_ratio is None
    nothing = energy_balance(production_kwh=0, consumption_kwh=10, grid_import_kwh=10,
                             grid_export_kwh=0)  # fmt: skip
    assert nothing.self_consumption_ratio is None and nothing.self_sufficiency_ratio == 0


def test_estimated_savings_uses_exact_decimal_arithmetic() -> None:
    assert estimated_savings(80, Decimal("55")) == Decimal("4400")
    assert estimated_savings(0.1 + 0.2, Decimal("10")) == Decimal("3")
    assert estimated_savings(12.345, Decimal("55.5")) == Decimal("685")  # 685.1475
    assert estimated_savings(80, None) is None


def test_linear_trend() -> None:
    trend = linear_trend([0, 1, 2, 3], [10, 8, 6, 4])
    assert trend is not None
    assert (trend.slope, trend.intercept, trend.r_squared) == pytest.approx((-2, 10, 1))
    noisy = linear_trend([0, 1, 2, 3, 4], [1, 3, 2, 4, 3])
    assert noisy is not None and 0 < noisy.r_squared < 1 and noisy.slope == pytest.approx(0.5)
    assert linear_trend([1, 2], [1, 2]) is None  # too few points
    assert linear_trend([5, 5, 5], [1, 2, 3]) is None  # no spread
    with pytest.raises(ValueError, match="same length"):
        linear_trend([1, 2, 3], [1, 2])
