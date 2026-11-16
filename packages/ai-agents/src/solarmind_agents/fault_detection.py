"""Agent 3 - Fault Detection: thresholds, trends and an optional statistical model.

Every anomaly carries its supporting evidence. Rule-based detections are the
primary signal. Isolation Forest is complementary only: it is an unsupervised,
unvalidated model, so its output is labelled "statistical" at INFO severity
and never raises an operational alert on its own.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from solarmind_agents.types import Finding
from solarmind_energy import linear_trend


@dataclass(frozen=True)
class ThresholdRule:
    id: uuid.UUID
    name: str
    metric: str
    operator: str  # "GT" or "LT"
    threshold: float
    severity: str
    category: str
    site_id: uuid.UUID | None = None  # None = every site of the organization

    def violated_by(self, value: float) -> bool:
        return value > self.threshold if self.operator == "GT" else value < self.threshold


@dataclass
class ThresholdHit:
    rule: ThresholdRule
    device_id: uuid.UUID
    row: Mapping[str, Any]  # the most recent violating measurement
    count: int = 0


def evaluate_thresholds(
    rows: Iterable[Mapping[str, Any]], rules: Sequence[ThresholdRule]
) -> list[ThresholdHit]:
    """Apply threshold rules to measurements.

    ``rows`` need ``device_id``, ``site_id``, ``metric_name``, ``value``,
    ``time`` and ``quality``. Flagged (non-valid) values never trigger a rule.
    One hit per (rule, device), pointing at the latest violating measurement.
    """
    by_metric: dict[str, list[ThresholdRule]] = {}
    for rule in rules:
        by_metric.setdefault(rule.metric, []).append(rule)
    hits: dict[tuple[uuid.UUID, uuid.UUID], ThresholdHit] = {}
    for row in rows:
        if row["quality"] != "valid":
            continue
        for rule in by_metric.get(row["metric_name"], ()):
            if rule.site_id is not None and rule.site_id != row["site_id"]:
                continue
            if not rule.violated_by(row["value"]):
                continue
            hit = hits.setdefault((rule.id, row["device_id"]),
                                  ThresholdHit(rule, row["device_id"], row))  # fmt: skip
            hit.count += 1
            if row["time"] > hit.row["time"]:
                hit.row = row
    return list(hits.values())


# --------------------------------------------------------------------------- underperformance

EXPECTED_PR = 0.75


def detect_underperformance(
    points: Sequence[tuple[datetime, float, float]], *, capacity_kwp: float,
    min_irradiance: float = 300.0, ratio_threshold: float = 0.6, min_consecutive: int = 3,
) -> Finding | None:  # fmt: skip
    """Flag sustained production far below what the measured irradiance allows.

    ``points`` are (time, pv_power_kw, irradiance_w_m2). Expected power is
    ``capacity x irradiance / 1000 x EXPECTED_PR``. Only daylight samples with
    enough irradiance are judged, so nights and dawn never trigger it.
    """
    if capacity_kwp <= 0:
        return None
    run: list[tuple[datetime, float, float]] = []
    worst: list[tuple[datetime, float, float]] = []
    for at, power, irradiance in sorted(points):
        expected = capacity_kwp * irradiance / 1000 * EXPECTED_PR
        if irradiance >= min_irradiance and power < expected * ratio_threshold:
            run.append((at, power, expected))
            if len(run) > len(worst):
                worst = list(run)
        else:
            run = []
    if len(worst) < min_consecutive:
        return None
    measured = sum(p for _, p, _ in worst) / len(worst)
    expected_mean = sum(e for _, _, e in worst) / len(worst)
    return Finding(
        code="underperformance",
        severity="HIGH",
        title="Production well below what irradiance allows",
        detail=(
            f"For {len(worst)} consecutive samples the plant delivered on average "
            f"{measured:.1f} kW where about {expected_mean:.1f} kW was expected."
        ),
        basis="calculated",
        evidence={
            "from": worst[0][0].isoformat(),
            "to": worst[-1][0].isoformat(),
            "samples": len(worst),
            "mean_measured_kw": round(measured, 2),
            "mean_expected_kw": round(expected_mean, 2),
            "ratio": round(measured / expected_mean, 3) if expected_mean else None,
        },  # fmt: skip
        assumptions=(
            f"Expected power assumes a performance ratio of {EXPECTED_PR}.",
            "The irradiance sensor is representative of the array and correctly calibrated.",
        ),
    )


# --------------------------------------------------------------------------- degradation trend


def detect_degradation(
    daily_ratio: Sequence[tuple[int, float]], *, min_points: int = 7,
    min_relative_drop: float = 0.08, min_r_squared: float = 0.5,
) -> Finding | None:  # fmt: skip
    """Detect a steady decline of a daily ratio (performance ratio, or specific
    yield when no irradiance reference exists).

    ``daily_ratio`` are (day index, ratio). A finding requires enough points,
    a clear linear fit and a material drop over the window.
    """
    if len(daily_ratio) < min_points:
        return None
    xs = [float(x) for x, _ in daily_ratio]
    ys = [y for _, y in daily_ratio]
    trend = linear_trend(xs, ys)
    if trend is None or trend.slope >= 0 or trend.r_squared < min_r_squared:
        return None
    start_value = trend.intercept + trend.slope * min(xs)
    end_value = trend.intercept + trend.slope * max(xs)
    if start_value <= 0:
        return None
    drop = (start_value - end_value) / start_value
    if drop < min_relative_drop:
        return None
    return Finding(
        code="degradation_trend",
        severity="WARNING" if drop < 0.2 else "HIGH",
        title="Gradual loss of performance",
        detail=(
            f"The daily performance indicator fell by about {drop:.0%} over "
            f"{int(max(xs) - min(xs)) + 1} days (linear fit, R2 = {trend.r_squared:.2f})."
        ),
        basis="calculated",
        evidence={
            "days": len(daily_ratio),
            "fitted_start": round(start_value, 4),
            "fitted_end": round(end_value, 4),
            "relative_drop": round(drop, 4),
            "slope_per_day": round(trend.slope, 6),
            "r_squared": round(trend.r_squared, 3),
        },  # fmt: skip
        assumptions=(
            "A linear fit describes the period; seasonal and weather effects are not modelled.",
            "Possible causes include soiling, shading, string failure or sensor drift: "
            "the trend alone does not identify which.",
        ),
    )


# --------------------------------------------------------------------------- isolation forest

MIN_SAMPLES_FOR_MODEL = 200


def isolation_forest_outliers(
    rows: Sequence[Mapping[str, Any]], features: Sequence[str], *,
    contamination: float = 0.01, max_findings: int = 5,
) -> list[Finding]:  # fmt: skip
    """Unsupervised outlier scan over ``features`` (complementary signal).

    Returns nothing when there is too little data to fit a model. Results are
    statistical curiosities to inspect, not validated fault diagnoses.
    """
    usable = [r for r in rows if all(r.get(f) is not None for f in features)]
    if len(usable) < MIN_SAMPLES_FOR_MODEL:
        return []
    import numpy as np
    from sklearn.ensemble import IsolationForest  # type: ignore[import-untyped]

    matrix = np.array([[float(r[f]) for f in features] for r in usable])
    model = IsolationForest(contamination=contamination, random_state=0, n_estimators=100)
    labels = model.fit_predict(matrix)
    scores = model.score_samples(matrix)
    outliers = sorted((i for i, label in enumerate(labels) if label == -1), key=lambda i: scores[i])
    findings = []
    for index in outliers[:max_findings]:
        row = usable[index]
        findings.append(
            Finding(
                code="statistical_outlier",
                severity="INFO",
                title="Unusual combination of measurements",
                detail=(
                    "This sample differs from the bulk of the period. It may be a transient, "
                    "a sensor glitch or a real fault: inspect before concluding."
                ),
                basis="statistical",
                evidence={
                    "time": row["time"].isoformat() if "time" in row else None,
                    "values": {f: round(float(row[f]), 3) for f in features},
                    "anomaly_score": round(float(scores[index]), 4),
                    "samples_in_model": len(usable),
                },
                assumptions=(
                    "Isolation Forest fitted on this period only; not validated against "
                    "labelled faults.",
                ),
            )
        )
    return findings
