"""Telemetry queries.

All SQL here is parameterized and scoped to one organization. The hourly
aggregate is read through ``telemetry_hourly_tenant`` (tenant-filtered view),
never the raw continuous aggregate.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.errors import AppError
from solarmind_schemas import METRICS

RAW_BUCKET_LIMIT = timedelta(days=3)


def check_metric(metric: str) -> None:
    if metric not in METRICS:
        raise AppError(f"Unknown metric '{metric}'", code="unknown_metric", status_code=422)


def check_range(start: datetime, end: datetime, max_days: int = 400) -> None:
    if end <= start:
        raise AppError("'end' must be after 'start'", code="invalid_range", status_code=422)
    if end - start > timedelta(days=max_days):
        raise AppError(
            f"Range too large (max {max_days} days)", code="invalid_range", status_code=422
        )


def pick_bucket(start: datetime, end: datetime) -> int:
    span = end - start
    if span <= timedelta(hours=36):
        return 300
    if span <= RAW_BUCKET_LIMIT:
        return 900
    if span <= timedelta(days=21):
        return 3600
    return 86400


async def latest(
    s: AsyncSession, org: uuid.UUID, *, site_id: uuid.UUID | None, device_id: uuid.UUID | None,
    since: datetime,
) -> list[dict[str, Any]]:  # fmt: skip
    """Most recent value of each (device, metric) newer than ``since``."""
    rows = await s.execute(
        text(
            """
            SELECT DISTINCT ON (device_id, metric_name)
                   device_id, site_id, metric_name, time, value, quality, source
            FROM telemetry
            WHERE organization_id = :org AND time >= :since
              AND (CAST(:site AS uuid) IS NULL OR site_id = :site)
              AND (CAST(:device AS uuid) IS NULL OR device_id = :device)
            ORDER BY device_id, metric_name, time DESC
            """
        ),
        {"org": org, "since": since, "site": site_id, "device": device_id},
    )
    return [dict(r._mapping) | {"unit": METRICS[r.metric_name].unit} for r in rows]


async def series(
    s: AsyncSession, org: uuid.UUID, *, site_id: uuid.UUID, metric: str, start: datetime,
    end: datetime, bucket_s: int | None = None, device_id: uuid.UUID | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Bucketed site-level series. Buckets without data are simply absent:
    a gap is reported as a gap, never as zero."""
    check_metric(metric)
    check_range(start, end)
    bucket_s = bucket_s or pick_bucket(start, end)
    combine = "sum" if METRICS[metric].site_aggregation == "sum" else "avg"
    params = {"org": org, "site": site_id, "metric": metric, "start": start, "end": end,
              "bucket": timedelta(seconds=bucket_s), "device": device_id}  # fmt: skip
    if bucket_s >= 3600:
        # Hourly aggregate, re-bucketed; device means are sample-weighted.
        per_device = """
            SELECT time_bucket(:bucket, bucket) AS b, device_id,
                   sum(avg_value * samples) / sum(samples) AS v,
                   min(min_value) AS lo, max(max_value) AS hi, sum(samples) AS n
            FROM telemetry_hourly_tenant
            WHERE organization_id = :org AND site_id = :site AND metric_name = :metric
              AND bucket >= :start AND bucket < :end
              AND (CAST(:device AS uuid) IS NULL OR device_id = :device)
            GROUP BY b, device_id
        """
    else:
        per_device = """
            SELECT time_bucket(:bucket, time) AS b, device_id,
                   avg(value) AS v, min(value) AS lo, max(value) AS hi, count(*) AS n
            FROM telemetry
            WHERE organization_id = :org AND site_id = :site AND metric_name = :metric
              AND time >= :start AND time < :end AND quality = 'valid'
              AND (CAST(:device AS uuid) IS NULL OR device_id = :device)
            GROUP BY b, device_id
        """
    rows = await s.execute(
        text(
            f"SELECT b, {combine}(v) AS value, min(lo) AS min, max(hi) AS max, sum(n) AS samples "  # noqa: S608
            f"FROM ({per_device}) d GROUP BY b ORDER BY b"
        ),
        params,
    )
    spec = METRICS[metric]
    return {
        "metric": metric,
        "unit": spec.unit,
        "bucket_seconds": bucket_s,
        "start": start,
        "end": end,
        "data_kind": "measured",
        "points": [
            {"time": r.b, "value": r.value, "min": r.min, "max": r.max, "samples": int(r.samples)}
            for r in rows
        ],
    }


async def raw(
    s: AsyncSession, org: uuid.UUID, *, device_id: uuid.UUID, metric: str | None, start: datetime,
    end: datetime, limit: int,
) -> list[dict[str, Any]]:  # fmt: skip
    check_range(start, end)
    rows = await s.execute(
        text(
            """
            SELECT time, device_id, metric_name, value, quality, source
            FROM telemetry
            WHERE organization_id = :org AND device_id = :device
              AND time >= :start AND time < :end
              AND (CAST(:metric AS text) IS NULL OR metric_name = :metric)
            ORDER BY time, metric_name
            LIMIT :limit
            """
        ),
        {"org": org, "device": device_id, "metric": metric, "start": start, "end": end,
         "limit": limit},
    )  # fmt: skip
    return [dict(r._mapping) for r in rows]
