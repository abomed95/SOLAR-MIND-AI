"""Agent 1 - Data Collector: data quality and communication health.

Purely descriptive: it counts what is there and what is missing. It never
repairs, interpolates or fills data.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from solarmind_agents.types import Reading
from solarmind_schemas import METRICS

Communication = Literal["ok", "degraded", "lost", "never_seen"]


@dataclass(frozen=True)
class Gap:
    start: datetime
    end: datetime
    missing_samples: int


@dataclass(frozen=True)
class QualityReport:
    metric: str
    unit: str | None
    expected_samples: int
    received_samples: int
    completeness: float  # share of expected sampling slots that hold data
    duplicates: int
    out_of_range: int
    flagged: int  # samples stored with a non-valid quality flag
    future_timestamps: int
    gaps: tuple[Gap, ...]
    last_sample_at: datetime | None
    communication: Communication
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "unit": self.unit,
            "expected_samples": self.expected_samples,
            "received_samples": self.received_samples,
            "completeness": round(self.completeness, 4),
            "duplicates": self.duplicates,
            "out_of_range": self.out_of_range,
            "flagged": self.flagged,
            "future_timestamps": self.future_timestamps,
            "gaps": [
                {
                    "start": g.start.isoformat(),
                    "end": g.end.isoformat(),
                    "missing_samples": g.missing_samples,
                }  # fmt: skip
                for g in self.gaps
            ],
            "last_sample_at": self.last_sample_at.isoformat() if self.last_sample_at else None,
            "communication": self.communication,
            "issues": list(self.issues),
        }


def assess_quality(
    metric: str, readings: Sequence[Reading], *, start: datetime, end: datetime,
    interval_s: int, now: datetime, max_future_skew_s: int = 300, stale_after_intervals: int = 3,
) -> QualityReport:  # fmt: skip
    """Assess one metric of one device over [start, end)."""
    spec = METRICS.get(metric)
    issues: list[str] = []
    if spec is None:
        issues.append(f"unknown metric '{metric}': unit and valid range cannot be checked")

    window_end = min(end, now)
    expected = max(0, int((window_end - start).total_seconds() // interval_s))
    ordered = sorted((r for r in readings if start <= r.time < end), key=lambda r: r.time)

    duplicates = sum(1 for a, b in zip(ordered, ordered[1:], strict=False) if a.time == b.time)
    out_of_range = (
        sum(1 for r in ordered if not spec.minimum <= r.value <= spec.maximum) if spec else 0
    )
    flagged = sum(1 for r in ordered if r.quality != "valid")
    future = sum(1 for r in readings if r.time > now + timedelta(seconds=max_future_skew_s))

    slots = {int((r.time - start).total_seconds() // interval_s) for r in ordered}
    completeness = min(1.0, len(slots) / expected) if expected else 0.0

    gaps = []
    tolerance = interval_s * 1.5  # jitter is not a gap
    edges = [start, *[r.time for r in ordered], window_end]
    for previous, current in zip(edges, edges[1:], strict=False):
        dt = (current - previous).total_seconds()
        if dt > tolerance:
            gaps.append(Gap(previous, current, max(1, round(dt / interval_s) - 1)))

    last = ordered[-1].time if ordered else None
    communication: Communication
    if last is None:
        communication = "never_seen"
    elif (now - last).total_seconds() > interval_s * stale_after_intervals:
        communication = "lost"
    elif completeness < 0.9:
        communication = "degraded"
    else:
        communication = "ok"

    if duplicates:
        issues.append(f"{duplicates} duplicate timestamp(s)")
    if out_of_range:
        issues.append(f"{out_of_range} value(s) outside the physical range of the metric")
    if future:
        issues.append(f"{future} timestamp(s) in the future: check the device clock")
    if gaps:
        missing = sum(g.missing_samples for g in gaps)
        issues.append(f"{len(gaps)} gap(s), about {missing} missing sample(s)")
    if communication == "lost":
        issues.append("no recent data: communication with the device appears lost")

    return QualityReport(
        metric=metric, unit=spec.unit if spec else None, expected_samples=expected,
        received_samples=len(ordered), completeness=completeness, duplicates=duplicates,
        out_of_range=out_of_range, flagged=flagged, future_timestamps=future, gaps=tuple(gaps),
        last_sample_at=last, communication=communication, issues=tuple(issues),
    )  # fmt: skip
