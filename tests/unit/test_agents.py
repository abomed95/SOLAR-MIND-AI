"""Agents 1 to 5: pure functions on in-memory fixtures."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from solarmind_adapters import Fault, FaultKind, PlantConfig, PlantModel, simulate
from solarmind_agents import data_collector, fault_detection, optimization, performance, predictive
from solarmind_agents.types import Reading

T0 = datetime(2026, 3, 10, 6, 0, tzinfo=UTC)


def readings(minutes: list[float], value: float = 10.0) -> list[Reading]:
    return [Reading(T0 + timedelta(minutes=m), value) for m in minutes]


# --------------------------------------------------------------------------- agent 1


def assess(rs: list[Reading], *, now_min: float = 60, metric: str = "pv_power_kw"):  # type: ignore[no-untyped-def]
    return data_collector.assess_quality(
        metric, rs, start=T0, end=T0 + timedelta(hours=1), interval_s=300,
        now=T0 + timedelta(minutes=now_min),
    )  # fmt: skip


def test_quality_of_a_complete_series() -> None:
    report = assess(readings(list(range(0, 60, 5))))
    assert (report.expected_samples, report.received_samples, report.completeness) == (12, 12, 1.0)
    assert report.communication == "ok" and report.issues == () and report.gaps == ()
    assert report.unit == "kW"


def test_quality_detects_gaps_duplicates_range_and_clock_errors() -> None:
    rs = readings([0, 5, 5, 10, 40, 45, 50, 55])  # duplicate at 5, hole 10 -> 40
    rs.append(Reading(T0 + timedelta(minutes=30), 9e9, quality="out_of_range"))
    rs.append(Reading(T0 + timedelta(hours=5), 1.0))  # device clock ahead
    report = assess(rs)
    assert report.duplicates == 1
    assert report.out_of_range == 1 and report.flagged == 1
    assert report.future_timestamps == 1
    assert [(g.start, g.end, g.missing_samples) for g in report.gaps] == [
        (T0 + timedelta(minutes=10), T0 + timedelta(minutes=30), 3),
        # The flagged sample at minute 30 exists, so it bounds a second, shorter gap.
        (T0 + timedelta(minutes=30), T0 + timedelta(minutes=40), 1),
    ]
    assert report.completeness == pytest.approx(8 / 12)
    assert report.communication == "degraded"
    assert len(report.issues) == 4


def test_quality_communication_states() -> None:
    assert assess([]).communication == "never_seen"
    assert assess([]).completeness == 0.0
    # Last sample 40 minutes before "now": more than 3 intervals -> lost.
    lost = assess(readings([0, 5, 10, 15, 20]), now_min=60)
    assert lost.communication == "lost"
    assert "communication" in lost.issues[-1]
    assert lost.gaps[-1].missing_samples == 7  # the trailing silence is a gap too


def test_quality_reports_unknown_metric_instead_of_guessing_a_unit() -> None:
    report = assess(readings([0, 5]), metric="made_up")
    assert report.unit is None and "unknown metric" in report.issues[0]


# --------------------------------------------------------------------------- agent 2


def day(n: int, production: float | None, insolation: float | None = 5.0, coverage: float = 0.45):  # type: ignore[no-untyped-def]
    return performance.DailyEnergy(date(2026, 3, n), production, production and production * 0.6,
                                   insolation, coverage)  # fmt: skip


def test_performance_summary_with_known_values() -> None:
    days = [day(1, 400), day(2, 300), day(3, None, None, 0.0), day(4, 500)]
    report = performance.analyze(days, capacity_kwp=100, previous=[day(5, 1000)])
    assert report["production_kwh"] == 1200
    assert report["days_with_data"] == 3 and report["days_in_period"] == 4
    assert report["average_daily_production_kwh"] == 400  # over days WITH data
    assert report["specific_yield_kwh_per_kwp"] == 12.0
    assert report["performance_ratio"] == pytest.approx(1200 / 100 / 15)
    assert report["best_day"] == {"day": "2026-03-04", "production_kwh": 500}
    assert report["worst_day"] == {"day": "2026-03-02", "production_kwh": 300}
    assert report["previous_period"]["production_change"] == pytest.approx(0.2)
    assert "not counted as zero" in report["notes"][0]


def test_performance_without_data_or_reference() -> None:
    empty = performance.analyze([day(1, None, None, 0.0)], capacity_kwp=100)
    assert empty["production_kwh"] is None and empty["performance_ratio"] is None
    no_sensor = performance.analyze([day(1, 400, None)], capacity_kwp=100)
    assert no_sensor["performance_ratio"] is None
    assert "irradiance" in no_sensor["notes"][0]


def test_site_comparison_ranks_by_specific_yield_not_size() -> None:
    ranking = performance.compare_sites([
        {"name": "Big", "capacity_kwp": 1000, "production_kwh": 4000, "insolation_kwh_m2": 5},
        {"name": "Small", "capacity_kwp": 50, "production_kwh": 250, "insolation_kwh_m2": 5},
        {"name": "Silent", "capacity_kwp": 80, "production_kwh": None},
    ])  # fmt: skip
    assert [(r["name"], r["rank"]) for r in ranking] == [("Small", 1), ("Big", 2), ("Silent", None)]
    assert ranking[0]["specific_yield_kwh_per_kwp"] == 5.0


# --------------------------------------------------------------------------- agent 3


def rule(metric: str, op: str, threshold: float, site: uuid.UUID | None = None):  # type: ignore[no-untyped-def]
    return fault_detection.ThresholdRule(uuid.uuid4(), "r", metric, op, threshold, "HIGH",
                                         "TEMPERATURE", site)  # fmt: skip


def row(device: uuid.UUID, site: uuid.UUID, metric: str, value: float, minute: int,
        quality: str = "valid") -> dict:  # type: ignore[type-arg]  # fmt: skip
    return {"device_id": device, "site_id": site, "metric_name": metric, "value": value,
            "time": T0 + timedelta(minutes=minute), "quality": quality, "source": "MEASURED"}  # fmt: skip


def test_thresholds_report_latest_violation_with_count() -> None:
    d1, d2, site = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hot, low = rule("inverter_temperature_c", "GT", 75), rule("battery_soc_percent", "LT", 15)
    rows = [
        row(d1, site, "inverter_temperature_c", 74.9, 0),
        row(d1, site, "inverter_temperature_c", 80.0, 5),
        row(d1, site, "inverter_temperature_c", 90.0, 10),
        row(d1, site, "inverter_temperature_c", 75.0, 15),  # equal is not above
        row(d2, site, "battery_soc_percent", 10.0, 5),
        row(d2, site, "inverter_temperature_c", 999.0, 5, quality="out_of_range"),
    ]
    hits = {(h.rule.metric, h.device_id): h for h in fault_detection.evaluate_thresholds(rows, [hot, low])}
    assert len(hits) == 2
    hit = hits[("inverter_temperature_c", d1)]
    assert (hit.count, hit.row["value"]) == (2, 90.0)
    assert hits[("battery_soc_percent", d2)].row["value"] == 10.0


def test_site_scoped_rule_ignores_other_sites() -> None:
    device, site_a, site_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scoped = rule("inverter_temperature_c", "GT", 50, site=site_a)
    rows = [row(device, site_b, "inverter_temperature_c", 60, 0)]
    assert fault_detection.evaluate_thresholds(rows, [scoped]) == []


def _plant_points(faults: list[Fault]) -> list[tuple[datetime, float, float]]:
    start = datetime(2026, 3, 9, 21, 0, tzinfo=UTC)
    states = simulate(PlantModel(PlantConfig(faults=faults)), start, start + timedelta(days=1),
                      timedelta(minutes=5))  # fmt: skip
    return [(s.at, s.values["pv_power_kw"], s.values["irradiance_w_m2"]) for s in states]


def test_underperformance_is_found_on_a_tripped_inverter_only() -> None:
    assert fault_detection.detect_underperformance(_plant_points([]), capacity_kwp=100) is None
    noon = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    trip = [Fault(FaultKind.INVERTER_TRIP, noon, noon + timedelta(hours=1))]
    finding = fault_detection.detect_underperformance(_plant_points(trip), capacity_kwp=100)
    assert finding is not None and finding.severity == "HIGH"
    assert finding.evidence["samples"] == 12
    assert finding.evidence["mean_measured_kw"] == 0.0
    assert finding.evidence["mean_expected_kw"] > 50
    assert finding.evidence["from"] == noon.isoformat()
    assert len(finding.assumptions) == 2


def test_underperformance_ignores_night_and_low_light() -> None:
    night = [(T0 + timedelta(minutes=5 * i), 0.0, 0.0) for i in range(50)]
    dawn = [(T0 + timedelta(minutes=5 * i), 0.0, 150.0) for i in range(50)]
    assert fault_detection.detect_underperformance(night + dawn, capacity_kwp=100) is None


def test_degradation_trend() -> None:
    steady = [(i, 0.80 + (0.004 if i % 2 else -0.004)) for i in range(14)]
    assert fault_detection.detect_degradation(steady) is None
    declining = [(i, 0.82 - 0.01 * i) for i in range(14)]
    finding = fault_detection.detect_degradation(declining)
    assert finding is not None and finding.code == "degradation_trend"
    assert finding.evidence["relative_drop"] == pytest.approx(0.13 / 0.82, abs=1e-3)
    assert finding.evidence["r_squared"] == 1.0
    assert finding.severity == "WARNING"
    assert fault_detection.detect_degradation(declining[:5]) is None  # too little history
    improving = [(i, 0.6 + 0.01 * i) for i in range(14)]
    assert fault_detection.detect_degradation(improving) is None
    noisy = [(i, 0.8 if i % 2 else 0.5) for i in range(14)]  # no clear line
    assert fault_detection.detect_degradation(noisy) is None


def test_isolation_forest_needs_data_and_labels_its_output_statistical() -> None:
    features = ("pv_ratio", "temperature")
    few = [{"pv_ratio": 0.5, "temperature": 40.0} for _ in range(50)]
    assert fault_detection.isolation_forest_outliers(few, features) == []

    rows = [{"time": T0 + timedelta(minutes=5 * i), "pv_ratio": 0.5 + 0.01 * (i % 7),
             "temperature": 40.0 + (i % 5)} for i in range(400)]  # fmt: skip
    rows[123] = {**rows[123], "pv_ratio": 0.02, "temperature": 95.0}
    first = fault_detection.isolation_forest_outliers(rows, features)
    assert first and first[0].evidence["time"] == rows[123]["time"].isoformat()
    assert {f.basis for f in first} == {"statistical"} and {f.severity for f in first} == {"INFO"}
    assert "not validated" in first[0].assumptions[0]
    again = fault_detection.isolation_forest_outliers(rows, features)
    assert [f.evidence for f in again] == [f.evidence for f in first]  # deterministic


# --------------------------------------------------------------------------- agent 4


def test_risk_requires_history() -> None:
    risk = predictive.assess_risk(predictive.MaintenanceInputs(history_days=5, critical_alerts_30d=9))
    assert risk.level == "INSUFFICIENT_DATA" and risk.score == 0
    assert "14" in risk.recommendation


def test_risk_levels_and_factors() -> None:
    quiet = predictive.assess_risk(predictive.MaintenanceInputs(history_days=60, availability_30d=0.99))
    assert (quiet.level, quiet.score, quiet.factors) == ("LOW", 0, ())

    medium = predictive.assess_risk(predictive.MaintenanceInputs(
        history_days=60, high_alerts_30d=2, communication_losses_30d=2))  # fmt: skip
    assert (medium.level, medium.score) == ("MEDIUM", 20)

    high = predictive.assess_risk(predictive.MaintenanceInputs(
        history_days=60, critical_alerts_30d=1, temperature_alerts_30d=3, degradation_drop=0.25,
        availability_30d=0.80, open_tickets=2))  # fmt: skip
    assert high.level == "HIGH" and high.score == 20 + 20 + 30 + 15
    assert [f["reason"] for f in high.factors][0].startswith("Critical alerts")
    assert all(f["evidence"] for f in high.factors)
    assert "2 maintenance ticket" in high.recommendation


def test_risk_never_claims_remaining_useful_life() -> None:
    text = str(predictive.assess_risk(predictive.MaintenanceInputs(
        history_days=90, critical_alerts_30d=5, degradation_drop=0.4)).as_dict()).lower()  # fmt: skip
    assert "not a prediction" in text
    for claim in ("will fail", "days until", "remaining life of"):
        assert claim not in text


# --------------------------------------------------------------------------- agent 5


def inputs(**overrides: object) -> optimization.OptimizationInputs:
    base = dict(period_days=30, production_kwh=1000.0, consumption_kwh=1500.0,
                grid_import_kwh=700.0, grid_export_kwh=50.0, performance_ratio=0.8,
                battery_capacity_kwh=200.0, battery_soc_min=25.0, battery_soc_max=95.0,
                tariff_per_kwh=Decimal("55"))  # fmt: skip
    return optimization.OptimizationInputs(**{**base, **overrides})  # type: ignore[arg-type]


def test_no_recommendation_for_a_healthy_site() -> None:
    assert optimization.recommend(inputs()) == []


def test_surplus_export_recommendation_states_its_assumptions() -> None:
    [finding] = optimization.recommend(inputs(grid_export_kwh=400.0))
    assert finding.code == "shift_or_store_surplus" and finding.basis == "estimated"
    # min(400 exported, 700 imported) x 55 DJF
    assert finding.evidence["estimated_saving"] == 22000
    assert finding.evidence["recoverable_kwh_upper_bound"] == 400.0
    assert any("Upper bound" in a for a in finding.assumptions)


def test_battery_and_performance_recommendations() -> None:
    codes = {f.code for f in optimization.recommend(inputs(
        battery_soc_min=92.0, battery_soc_max=100.0, performance_ratio=0.55,
        grid_import_kwh=1400.0))}  # fmt: skip
    assert codes == {"battery_underused", "low_performance_ratio", "high_grid_dependence"}
    deep = {f.code for f in optimization.recommend(inputs(battery_soc_min=8.0))}
    assert deep == {"battery_deep_discharge"}
    for finding in optimization.recommend(inputs(performance_ratio=0.5)):
        assert finding.assumptions and finding.evidence


def test_missing_inputs_produce_no_recommendation() -> None:
    blank = inputs(production_kwh=None, consumption_kwh=None, grid_import_kwh=None,
                   grid_export_kwh=None, performance_ratio=None, battery_capacity_kwh=None,
                   battery_soc_min=None, battery_soc_max=None, tariff_per_kwh=None)  # fmt: skip
    assert optimization.recommend(blank) == []


def test_maintenance_prioritization() -> None:
    ranked = optimization.prioritize_maintenance([
        {"id": "old-warning", "severity": "WARNING", "occurrence_count": 3, "age_hours": 400},
        {"id": "new-critical", "severity": "CRITICAL", "occurrence_count": 1, "age_hours": 1},
        {"id": "info", "severity": "INFO", "occurrence_count": 1, "age_hours": 1},
    ])  # fmt: skip
    assert [a["id"] for a in ranked] == ["new-critical", "old-warning", "info"]
    assert [a["priority_rank"] for a in ranked] == [1, 2, 3]
