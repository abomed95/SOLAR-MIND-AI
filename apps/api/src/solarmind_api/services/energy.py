"""Energy KPIs computed from stored telemetry.

Integration runs in SQL (trapezoids over consecutive valid samples, per
device) with the same gap rule as ``solarmind_energy.integrate``; a test
asserts both agree. Ratios and estimates come from ``solarmind_energy``.

Every figure is tagged with its nature:
``measured`` (read from a device), ``calculated`` (derived from measurements),
``estimated`` (depends on stated assumptions). A figure without data is
``None``, never zero.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.enums import AlertStatus
from solarmind_api.models import Alert, Device, Site
from solarmind_api.services.telemetry import check_range
from solarmind_energy import (
    energy_balance,
    estimated_savings,
    performance_ratio,
    specific_yield,
)

# An interval between two samples longer than this many sampling periods is a
# gap: no energy is attributed to it.
GAP_FACTOR = 3
ENERGY_METRICS = ("pv_power_kw", "load_power_kw", "grid_import_kw", "grid_export_kw",
                  "irradiance_w_m2")  # fmt: skip
SAVINGS_ASSUMPTIONS = [
    "Every self-consumed kWh would otherwise have been bought from the grid.",
    "Flat tariff equal to the site's configured tariff; no demand charges.",
    "No revenue is counted for exported energy.",
]

_DAILY_SQL = text(
    """
    WITH samples AS (
        SELECT t.site_id, t.device_id, t.metric_name, t.time, t.value,
               lag(t.time)  OVER w AS prev_time,
               lag(t.value) OVER w AS prev_value,
               st.timezone, st.expected_interval_s * :gap_factor AS max_gap
        FROM telemetry t
        JOIN sites st ON st.id = t.site_id
        WHERE t.organization_id = :org
          AND t.site_id = ANY(CAST(:sites AS uuid[]))
          AND t.metric_name = ANY(CAST(:metrics AS text[]))
          AND t.time >= :start AND t.time < :end
          AND t.quality = 'valid'
        WINDOW w AS (PARTITION BY t.device_id, t.metric_name ORDER BY t.time)
    ), per_device AS (
        -- An interval is attributed to the local day of its closing sample.
        SELECT site_id, device_id, metric_name,
               (time AT TIME ZONE timezone)::date AS day,
               sum((value + prev_value) / 2 * extract(epoch FROM time - prev_time) / 3600) AS total,
               sum(extract(epoch FROM time - prev_time)) AS covered_s
        FROM samples
        WHERE prev_time IS NOT NULL
          AND extract(epoch FROM time - prev_time) <= max_gap
        GROUP BY site_id, device_id, metric_name, day
    )
    SELECT site_id, metric_name, day,
           sum(total) AS sum_total, avg(total) AS avg_total, avg(covered_s) AS covered_s
    FROM per_device
    GROUP BY site_id, metric_name, day
    ORDER BY day
    """
)


def _pairs(rows: Any) -> dict[Any, Any]:
    """Two-column result rows as a mapping."""
    return {key: value for key, value in rows}


@dataclass(frozen=True)
class DailyValue:
    site_id: uuid.UUID
    metric: str
    day: date
    total: float  # kWh, or kWh/m2 for irradiance
    covered_s: float


async def daily_integrals(
    s: AsyncSession, org: uuid.UUID, site_ids: list[uuid.UUID], start: datetime, end: datetime
) -> list[DailyValue]:
    if not site_ids:
        return []
    rows = await s.execute(
        _DAILY_SQL,
        {"org": org, "sites": site_ids, "metrics": list(ENERGY_METRICS), "start": start,
         "end": end, "gap_factor": GAP_FACTOR},
    )  # fmt: skip
    out = []
    for r in rows:
        if r.metric_name == "irradiance_w_m2":
            # Several sensors measure the same sun: average them. Wh/m2 -> kWh/m2.
            total = float(r.avg_total) / 1000
        else:
            total = float(r.sum_total)  # several devices add up
        out.append(DailyValue(r.site_id, r.metric_name, r.day, total, float(r.covered_s)))
    return out


def _figure(value: float | None, kind: str, unit: str, **extra: Any) -> dict[str, Any]:
    return {
        "value": None if value is None else round(value, 3),
        "unit": unit,
        "kind": kind,
        **extra,
    }


def _local_midnight(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _totals(values: list[DailyValue]) -> tuple[dict[str, float], dict[str, float]]:
    totals: dict[str, float] = defaultdict(float)
    covered: dict[str, float] = defaultdict(float)
    for v in values:
        totals[v.metric] += v.total
        covered[v.metric] += v.covered_s
    return totals, covered


def kpis(site: Site, values: list[DailyValue], window_s: float) -> dict[str, Any]:
    """Period KPIs of one site from its daily integrals."""
    totals, covered = _totals(values)

    def energy(metric: str) -> dict[str, Any]:
        present = metric in totals
        coverage = min(1.0, covered[metric] / window_s) if present and window_s > 0 else 0.0
        return _figure(totals[metric] if present else None, "calculated", "kWh",
                       coverage=round(coverage, 4))  # fmt: skip

    production = totals.get("pv_power_kw")
    out: dict[str, Any] = {
        "production": energy("pv_power_kw"),
        "consumption": energy("load_power_kw"),
        "grid_import": energy("grid_import_kw"),
        "grid_export": energy("grid_export_kw"),
    }
    if production is None:
        empty = {"value": None, "kind": "calculated", "note": "no production data in this period"}
        out.update(self_consumption=empty, self_sufficiency=empty, specific_yield=empty,
                   performance_ratio=empty,
                   estimated_savings={**empty, "kind": "estimated"})  # fmt: skip
        return out

    balance = energy_balance(
        production_kwh=production,
        consumption_kwh=totals.get("load_power_kw"),
        grid_import_kwh=totals.get("grid_import_kw"),
        grid_export_kwh=totals.get("grid_export_kw"),
    )
    export_known = "grid_export_kw" in totals
    out["self_consumption"] = _figure(
        balance.self_consumed_kwh, "calculated", "kWh", ratio=balance.self_consumption_ratio,
        note=None if export_known else "no export measurement: all production assumed used on site",
    )  # fmt: skip
    out["self_sufficiency"] = _figure(balance.self_sufficiency_ratio, "calculated", "ratio")
    out["specific_yield"] = _figure(
        specific_yield(production, site.installed_capacity_kwp), "calculated", "kWh/kWp"
    )
    insolation = totals.get("irradiance_w_m2")
    ratio = performance_ratio(production, insolation or 0.0, site.installed_capacity_kwp)
    out["performance_ratio"] = _figure(
        ratio, "calculated", "ratio",
        insolation_kwh_m2=None if insolation is None else round(insolation, 3),
        note=None if ratio is not None else "no irradiance measurement available as reference",
    )  # fmt: skip
    savings = estimated_savings(balance.self_consumed_kwh, site.tariff_per_kwh)
    out["estimated_savings"] = {
        "value": None if savings is None else int(savings),
        "unit": site.currency,
        "kind": "estimated",
        "assumptions": SAVINGS_ASSUMPTIONS if savings is not None else [],
        "note": None if savings is not None else "no tariff configured for this site",
    }
    return out


async def _availability(
    s: AsyncSession, org: uuid.UUID, site: Site, start: datetime, end: datetime
) -> float | None:
    """Share of expected sampling slots in which the site reported PV power."""
    slots = int((end - start).total_seconds() // site.expected_interval_s)
    if slots <= 0:
        return None
    seen = await s.scalar(
        text(
            "SELECT count(DISTINCT time_bucket(:bucket, time, CAST(:start AS timestamptz))) "
            "FROM telemetry WHERE organization_id = :org AND site_id = :site "
            "AND metric_name = 'pv_power_kw' AND time >= :start AND time < :end"
        ),
        {"org": org, "site": site.id, "start": start, "end": end,
         "bucket": timedelta(seconds=site.expected_interval_s)},
    )  # fmt: skip
    return min(1.0, (seen or 0) / slots)


async def site_summary(
    s: AsyncSession, org: uuid.UUID, site: Site, start: datetime, end: datetime
) -> dict[str, Any]:
    check_range(start, end)
    effective_end = min(end, datetime.now(UTC))
    window_s = max(0.0, (effective_end - start).total_seconds())
    values = await daily_integrals(s, org, [site.id], start, end)
    quality = _pairs(
        (
            await s.execute(
                text(
                    "SELECT quality, count(*) FROM telemetry WHERE organization_id = :org "
                    "AND site_id = :site AND time >= :start AND time < :end GROUP BY quality"
                ),
                {"org": org, "site": site.id, "start": start, "end": end},
            )
        ).all()
    )
    battery = (
        await s.execute(
            text(
                "SELECT min(value), avg(value), max(value) FROM telemetry "
                "WHERE organization_id = :org AND site_id = :site AND quality = 'valid' "
                "AND metric_name = 'battery_soc_percent' AND time >= :start AND time < :end"
            ),
            {"org": org, "site": site.id, "start": start, "end": end},
        )
    ).one()
    availability = await _availability(s, org, site, start, effective_end)
    return {
        "site_id": site.id,
        "site_name": site.name,
        "period": {"start": start, "end": end},
        "simulated": site.is_simulated,
        **kpis(site, values, window_s),
        "availability": _figure(availability, "calculated", "ratio"),
        "battery_soc": {
            "min": battery[0],
            "avg": None if battery[1] is None else round(battery[1], 2),
            "max": battery[2],
            "unit": "%",
            "kind": "measured",
        },  # fmt: skip
        "data_quality": {
            "valid": quality.get("valid", 0),
            "out_of_range": quality.get("out_of_range", 0),
            "suspect": quality.get("suspect", 0),
            "note": "Energy figures use valid samples only; gaps longer than "
            f"{GAP_FACTOR} sampling intervals carry no energy.",
        },
    }


async def site_daily(
    s: AsyncSession, org: uuid.UUID, site: Site, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    check_range(start, end)
    by_day: dict[date, dict[str, Any]] = {}
    names = {"pv_power_kw": "production_kwh", "load_power_kw": "consumption_kwh",
             "grid_import_kw": "grid_import_kwh", "grid_export_kw": "grid_export_kwh",
             "irradiance_w_m2": "insolation_kwh_m2"}  # fmt: skip
    for v in await daily_integrals(s, org, [site.id], start, end):
        row = by_day.setdefault(v.day, {"day": v.day, "kind": "calculated"})
        row[names[v.metric]] = round(v.total, 3)
        if v.metric == "pv_power_kw":
            row["coverage"] = round(min(1.0, v.covered_s / 86400), 4)
    return [by_day[d] for d in sorted(by_day)]


async def overview(s: AsyncSession, org: uuid.UUID) -> dict[str, Any]:
    """Organization dashboard: live figures, today, this month, per-site table."""
    now = datetime.now(UTC)
    sites = list((await s.scalars(select(Site).where(Site.organization_id == org))).all())
    month_start = {site.id: _local_midnight(now, site.timezone).replace(day=1) for site in sites}
    today_start = {site.id: _local_midnight(now, site.timezone) for site in sites}
    values = await daily_integrals(
        s, org, [site.id for site in sites], min(month_start.values(), default=now), now
    )
    by_site: dict[uuid.UUID, list[DailyValue]] = defaultdict(list)
    for v in values:
        by_site[v.site_id].append(v)

    # Live values: only readings fresh enough to be called "current".
    latest_rows = (
        await s.execute(
            text(
                """
                SELECT DISTINCT ON (t.device_id, t.metric_name)
                       t.site_id, t.metric_name, t.value, t.time,
                       now() - t.time <= make_interval(secs => st.expected_interval_s * :f) AS fresh
                FROM telemetry t JOIN sites st ON st.id = t.site_id
                WHERE t.organization_id = :org AND t.time >= :since AND t.quality = 'valid'
                  AND t.metric_name IN ('pv_power_kw', 'load_power_kw', 'battery_soc_percent',
                                        'total_energy_kwh')
                ORDER BY t.device_id, t.metric_name, t.time DESC
                """
            ),
            {"org": org, "since": now - timedelta(days=7), "f": GAP_FACTOR},
        )
    ).all()
    live: dict[uuid.UUID, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    lifetime: dict[uuid.UUID, float] = defaultdict(float)
    for r in latest_rows:
        if r.metric_name == "total_energy_kwh":
            lifetime[r.site_id] += r.value  # counters stay meaningful when stale
        elif r.fresh:
            live[r.site_id][r.metric_name].append(r.value)

    alert_counts = _pairs(
        (
            await s.execute(
                select(Alert.severity, func.count())
                .where(Alert.organization_id == org, Alert.status != AlertStatus.RESOLVED)
                .group_by(Alert.severity)
            )
        ).all()
    )
    site_alerts = _pairs(
        (
            await s.execute(
                select(Alert.site_id, func.count())
                .where(Alert.organization_id == org, Alert.status != AlertStatus.RESOLVED)
                .group_by(Alert.site_id)
            )
        ).all()
    )

    rows: list[dict[str, Any]] = []
    tot: dict[str, float] = defaultdict(float)
    have: dict[str, bool] = defaultdict(bool)
    savings_total, coverages = 0, []
    for site in sites:
        month = [v for v in by_site[site.id] if v.day >= month_start[site.id].date()]
        today = [v for v in month if v.day == today_start[site.id].date()]
        today_window = (now - today_start[site.id]).total_seconds()
        month_window = (now - month_start[site.id]).total_seconds()
        k_today, k_month = kpis(site, today, today_window), kpis(site, month, month_window)
        pv_now = sum(live[site.id]["pv_power_kw"]) if live[site.id]["pv_power_kw"] else None
        load_now = sum(live[site.id]["load_power_kw"]) if live[site.id]["load_power_kw"] else None
        socs = live[site.id]["battery_soc_percent"]
        soc = sum(socs) / len(socs) if socs else None
        for key, value in (("pv_now", pv_now), ("load_now", load_now),
                           ("prod_today", k_today["production"]["value"]),
                           ("cons_today", k_today["consumption"]["value"]),
                           ("prod_month", k_month["production"]["value"]),
                           ("lifetime", lifetime.get(site.id))):  # fmt: skip
            if value is not None:
                tot[key] += value
                have[key] = True
        if k_month["estimated_savings"]["value"] is not None:
            savings_total += k_month["estimated_savings"]["value"]
            have["savings"] = True
        if k_today["production"]["value"] is not None:
            coverages.append(k_today["production"]["coverage"])
        rows.append({
            "site_id": site.id, "name": site.name, "kind": site.kind,
            "simulated": site.is_simulated, "installed_capacity_kwp": site.installed_capacity_kwp,
            "current_power_kw": None if pv_now is None else round(pv_now, 2),
            "battery_soc_percent": None if soc is None else round(soc, 1),
            "production_today_kwh": k_today["production"]["value"],
            "production_month_kwh": k_month["production"]["value"],
            "specific_yield_today": k_today["specific_yield"]["value"],
            "performance_ratio_month": k_month["performance_ratio"]["value"],
            "coverage_today": k_today["production"].get("coverage"),
            "active_alerts": site_alerts.get(site.id, 0),
            "reporting": pv_now is not None,
        })  # fmt: skip

    def total(key: str, kind: str, unit: str) -> dict[str, Any]:
        return _figure(tot[key] if have[key] else None, kind, unit)

    all_socs = [r["battery_soc_percent"] for r in rows if r["battery_soc_percent"] is not None]
    currencies = {site.currency for site in sites}
    return {
        "generated_at": now,
        "simulated": any(site.is_simulated for site in sites),
        "site_count": len(sites),
        "reporting_sites": sum(1 for r in rows if r["reporting"]),
        "current_power": total("pv_now", "measured", "kW"),
        "current_consumption": total("load_now", "measured", "kW"),
        "battery_soc": _figure(
            sum(all_socs) / len(all_socs) if all_socs else None, "measured", "%"
        ),
        "production_today": total("prod_today", "calculated", "kWh"),
        "consumption_today": total("cons_today", "calculated", "kWh"),
        "production_month": total("prod_month", "calculated", "kWh"),
        "total_production": {
            **total("lifetime", "measured", "kWh"),
            "note": "Sum of the inverters' lifetime energy counters.",
        },  # fmt: skip
        "availability_today": _figure(
            sum(coverages) / len(coverages) if coverages else None,
            "calculated",
            "ratio",
            note="Share of today's elapsed time backed by PV power samples.",
        ),  # fmt: skip
        "estimated_savings_month": {
            "value": savings_total if have["savings"] else None,
            "unit": currencies.pop() if len(currencies) == 1 else "mixed",
            "kind": "estimated",
            "assumptions": SAVINGS_ASSUMPTIONS,
        },
        "active_alerts": {
            sev: alert_counts.get(sev, 0) for sev in ("CRITICAL", "HIGH", "WARNING", "INFO")
        },  # fmt: skip
        "sites": sorted(rows, key=lambda r: r["name"]),
    }


async def equipment_health(s: AsyncSession, org: uuid.UUID) -> list[dict[str, Any]]:
    """Per-device connection status, 24 h data availability and open alerts."""
    now = datetime.now(UTC)
    since = now - timedelta(hours=24)
    buckets = _pairs(
        (
            await s.execute(
                text(
                    """
                    SELECT t.device_id,
                           count(DISTINCT time_bucket(make_interval(secs => st.expected_interval_s),
                                                      t.time))
                    FROM telemetry t JOIN sites st ON st.id = t.site_id
                    WHERE t.organization_id = :org AND t.time >= :since
                    GROUP BY t.device_id
                    """
                ),
                {"org": org, "since": since},
            )
        ).all()
    )
    open_alerts = _pairs(
        (
            await s.execute(
                select(Alert.device_id, func.count())
                .where(Alert.organization_id == org, Alert.status != AlertStatus.RESOLVED)
                .group_by(Alert.device_id)
            )
        ).all()
    )
    rows = await s.execute(
        select(Device, Site.name, Site.expected_interval_s)
        .join(Site, Site.id == Device.site_id)
        .where(Device.organization_id == org)
        .order_by(Site.name, Device.name)
    )
    out = []
    for device, site_name, interval_s in rows:
        if device.last_seen_at is None:
            status = "NEVER_SEEN"
        elif now - device.last_seen_at <= timedelta(seconds=interval_s * GAP_FACTOR):
            status = "ONLINE"
        else:
            status = "OFFLINE"
        expected = 86400 // interval_s
        out.append({
            "device_id": device.id, "name": device.name, "kind": device.kind,
            "site_id": device.site_id, "site_name": site_name, "status": status,
            "last_seen_at": device.last_seen_at, "simulated": device.is_simulated,
            "availability_24h": round(min(1.0, buckets.get(device.id, 0) / expected), 4),
            "open_alerts": open_alerts.get(device.id, 0),
        })  # fmt: skip
    return out
