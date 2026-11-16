"""Bridges the agents to stored data, and exposes it to the assistant as tools.

Every function takes the caller's tenant transaction and organization. The
assistant's tools are closures over them: the model supplies at most a site
identifier, which is looked up *inside* the caller's organization, so it can
never reach another tenant's rows or run a query of its own.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_agents import data_collector, fault_detection, optimization, performance, predictive
from solarmind_agents.assistant import (
    Assistant,
    DeterministicProvider,
    LLMProvider,
    OpenAIProvider,
    Tool,
    ToolError,
)
from solarmind_agents.types import Reading
from solarmind_api.config import Settings
from solarmind_api.deps import AuthContext
from solarmind_api.enums import AlertCategory, AlertStatus, Severity, TicketStatus
from solarmind_api.errors import NotFound
from solarmind_api.models import (
    AIConversation,
    AIMessage,
    Alert,
    Device,
    MaintenanceTicket,
    Site,
)
from solarmind_api.permissions import P
from solarmind_api.plans import Feature
from solarmind_api.services import billing, energy
from solarmind_api.services.audit import audit
from solarmind_energy import performance_ratio, specific_yield
from solarmind_schemas import METRICS

PRIMARY_METRIC = {
    "INVERTER": "pv_power_kw",
    "BATTERY_BMS": "battery_soc_percent",
    "METER": "load_power_kw",
    "WEATHER_STATION": "irradiance_w_m2",
}
MIN_DAY_COVERAGE = 0.2


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(days=days), end


# --------------------------------------------------------------------------- agent inputs


async def daily_energy(
    s: AsyncSession, org: uuid.UUID, site: Site, start: datetime, end: datetime
) -> list[performance.DailyEnergy]:
    rows = await energy.site_daily(s, org, site, start, end)
    return [
        performance.DailyEnergy(
            day=r["day"],
            production_kwh=r.get("production_kwh"),
            consumption_kwh=r.get("consumption_kwh"),
            insolation_kwh_m2=r.get("insolation_kwh_m2"),
            coverage=r.get("coverage", 0.0),
        )  # fmt: skip
        for r in rows
    ]


async def data_quality(
    s: AsyncSession, org: uuid.UUID, site: Site, hours: int = 24
) -> dict[str, Any]:
    """Agent 1 over the primary metric of each device of the site."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    devices = await s.scalars(
        select(Device)
        .where(Device.organization_id == org, Device.site_id == site.id)
        .order_by(Device.name)
    )
    reports = []
    for device in devices:
        metric = PRIMARY_METRIC.get(device.kind)
        if metric is None:
            continue
        rows = await s.execute(
            text(
                "SELECT time, value, quality FROM telemetry WHERE organization_id = :org "
                "AND device_id = :device AND metric_name = :metric "
                "AND time >= :start AND time < :end ORDER BY time"
            ),
            {"org": org, "device": device.id, "metric": metric, "start": start, "end": end},
        )
        report = data_collector.assess_quality(
            metric, [Reading(r.time, r.value, r.quality) for r in rows], start=start, end=end,
            interval_s=site.expected_interval_s, now=end,
        )  # fmt: skip
        reports.append({"device": device.name, "device_id": device.id, **report.as_dict()})
    return {"site": site.name, "simulated": site.is_simulated,
            "period": {"start": start, "end": end}, "reports": reports}  # fmt: skip


async def performance_report(
    s: AsyncSession, org: uuid.UUID, site: Site, days: int
) -> dict[str, Any]:
    """Agent 2: the period against the one just before it."""
    start, end = _window(days)
    current = await daily_energy(s, org, site, start, end)
    previous = await daily_energy(s, org, site, start - timedelta(days=days), start)
    report = performance.analyze(
        current, capacity_kwp=site.installed_capacity_kwp, previous=previous
    )
    return {"site": site.name, "simulated": site.is_simulated,
            "period": {"start": start, "end": end}, **report}  # fmt: skip


def _daily_indicator(
    days: list[performance.DailyEnergy], capacity_kwp: float
) -> tuple[list[tuple[int, float]], str]:
    """Daily performance ratio, or specific yield when no irradiance exists."""
    usable = [d for d in days if d.production_kwh is not None and d.coverage >= MIN_DAY_COVERAGE]
    if not usable:
        return [], "none"
    origin = usable[0].day
    with_sun = [d for d in usable if d.insolation_kwh_m2 and d.insolation_kwh_m2 > 0.5]
    if len(with_sun) >= len(usable) * 0.8:
        points = [
            ((d.day - origin).days,
             performance_ratio(d.production_kwh or 0, d.insolation_kwh_m2 or 0, capacity_kwp))
            for d in with_sun
        ]  # fmt: skip
        return [(x, y) for x, y in points if y is not None], "performance_ratio"
    points = [((d.day - origin).days, specific_yield(d.production_kwh or 0, capacity_kwp))
              for d in usable]  # fmt: skip
    return [(x, y) for x, y in points if y is not None], "specific_yield"


async def anomalies(
    s: AsyncSession, org: uuid.UUID, site: Site, *, statistical: bool
) -> dict[str, Any]:
    """Agent 3 beyond thresholds: underperformance, degradation, outliers."""
    start, end = _window(7)
    rows = (
        await s.execute(
            text(
                """
                SELECT time,
                       sum(value) FILTER (WHERE metric_name = 'pv_power_kw') AS pv,
                       avg(value) FILTER (WHERE metric_name = 'irradiance_w_m2') AS irradiance,
                       avg(value) FILTER (WHERE metric_name = 'inverter_temperature_c') AS temp
                FROM telemetry
                WHERE organization_id = :org AND site_id = :site AND quality = 'valid'
                  AND time >= :start AND time < :end
                  AND metric_name IN ('pv_power_kw', 'irradiance_w_m2', 'inverter_temperature_c')
                GROUP BY time ORDER BY time
                """
            ),
            {"org": org, "site": site.id, "start": start, "end": end},
        )
    ).all()
    findings = []
    paired = [
        (r.time, r.pv, r.irradiance) for r in rows if r.pv is not None and r.irradiance is not None
    ]
    under = fault_detection.detect_underperformance(
        paired, capacity_kwp=site.installed_capacity_kwp
    )
    if under:
        findings.append(under.as_dict())

    month_start, _ = _window(30)
    indicator, kind = _daily_indicator(
        await daily_energy(s, org, site, month_start, end), site.installed_capacity_kwp
    )
    degradation = fault_detection.detect_degradation(indicator)
    if degradation:
        finding = degradation.as_dict()
        finding["evidence"]["indicator"] = kind
        if kind == "specific_yield":
            finding["assumptions"].append(
                "No irradiance reference: the trend uses specific yield, which also falls "
                "with cloudier weather."
            )
        findings.append(finding)

    notes = []
    if statistical:
        features = [
            {
                "time": r.time,
                "pv_ratio": r.pv / site.installed_capacity_kwp,
                "irradiance": r.irradiance,
                "inverter_temperature": r.temp,
            }  # fmt: skip
            for r in rows
            if r.pv is not None
            and r.irradiance is not None
            and r.temp is not None
            and r.irradiance > 50
        ]
        outliers = fault_detection.isolation_forest_outliers(
            features, ("pv_ratio", "irradiance", "inverter_temperature")
        )
        findings += [f.as_dict() for f in outliers]
        if not outliers and len(features) < fault_detection.MIN_SAMPLES_FOR_MODEL:
            notes.append("Too few daylight samples for the statistical scan.")
    else:
        notes.append("Statistical anomaly scan is not included in the current plan.")
    return {"site": site.name, "simulated": site.is_simulated,
            "period": {"start": start, "end": end}, "findings": findings, "notes": notes,
            "degradation": degradation}  # fmt: skip


async def maintenance_risk(s: AsyncSession, org: uuid.UUID, site: Site) -> dict[str, Any]:
    """Agent 4 from 30 days of alerts, availability and the degradation rule."""
    start, end = _window(30)
    first = await s.scalar(
        text("SELECT min(time) FROM telemetry WHERE organization_id = :org AND site_id = :site"),
        {"org": org, "site": site.id},
    )
    history_days = (end - first).days if first else 0

    async def alerts(*where: Any) -> int:
        return (
            await s.scalar(
                select(func.count())
                .select_from(Alert)
                .where(
                    Alert.organization_id == org,
                    Alert.site_id == site.id,
                    Alert.first_seen_at >= start,
                    *where,
                )  # fmt: skip
            )
            or 0
        )

    indicator, _ = _daily_indicator(
        await daily_energy(s, org, site, start, end), site.installed_capacity_kwp
    )
    degradation = fault_detection.detect_degradation(indicator)
    availability = await energy._availability(s, org, site, max(start, first or start), end)
    open_tickets = await s.scalar(
        select(func.count())
        .select_from(MaintenanceTicket)
        .where(
            MaintenanceTicket.organization_id == org,
            MaintenanceTicket.site_id == site.id,
            MaintenanceTicket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]),
        )  # fmt: skip
    )
    risk = predictive.assess_risk(
        predictive.MaintenanceInputs(
            history_days=history_days,
            critical_alerts_30d=await alerts(Alert.severity == Severity.CRITICAL),
            high_alerts_30d=await alerts(Alert.severity == Severity.HIGH),
            temperature_alerts_30d=await alerts(Alert.category == AlertCategory.TEMPERATURE),
            communication_losses_30d=await alerts(Alert.category == AlertCategory.COMMUNICATION),
            degradation_drop=degradation.evidence["relative_drop"] if degradation else None,
            availability_30d=availability,
            open_tickets=open_tickets or 0,
        )
    )
    return {"site": site.name, "simulated": site.is_simulated,
            "period": {"start": start, "end": end}, "history_days": history_days,
            **risk.as_dict()}  # fmt: skip


async def recommendations(
    s: AsyncSession, org: uuid.UUID, site: Site, days: int = 30
) -> dict[str, Any]:
    """Agent 5 from the period's energy balance and battery behaviour."""
    start, end = _window(days)
    summary = await energy.site_summary(s, org, site, start, end)
    findings = optimization.recommend(
        optimization.OptimizationInputs(
            period_days=days,
            production_kwh=summary["production"]["value"],
            consumption_kwh=summary["consumption"]["value"],
            grid_import_kwh=summary["grid_import"]["value"],
            grid_export_kwh=summary["grid_export"]["value"],
            performance_ratio=summary["performance_ratio"]["value"],
            battery_capacity_kwh=site.battery_capacity_kwh,
            battery_soc_min=summary["battery_soc"]["min"],
            battery_soc_max=summary["battery_soc"]["max"],
            tariff_per_kwh=site.tariff_per_kwh,
            currency=site.currency,
        )
    )
    return {"site": site.name, "simulated": site.is_simulated, "period_days": days,
            "period": {"start": start, "end": end},
            "recommendations": [f.as_dict() for f in findings]}  # fmt: skip


async def prioritized_alerts(s: AsyncSession, org: uuid.UUID) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    rows = await s.execute(
        select(Alert, Site.name)
        .join(Site, Site.id == Alert.site_id)
        .where(Alert.organization_id == org, Alert.status != AlertStatus.RESOLVED)
    )
    return optimization.prioritize_maintenance([
        {"id": a.id, "title": a.title, "severity": a.severity, "site": name,
         "status": a.status, "occurrence_count": a.occurrence_count,
         "age_hours": round((now - a.first_seen_at).total_seconds() / 3600, 1)}
        for a, name in rows
    ])  # fmt: skip


async def site_insights(
    s: AsyncSession, org: uuid.UUID, site: Site, *, statistical: bool
) -> dict[str, Any]:
    """Everything agents 1 to 5 have to say about a site."""
    found = await anomalies(s, org, site, statistical=statistical)
    found.pop("degradation")
    return {
        "site_id": site.id,
        "site": site.name,
        "simulated": site.is_simulated,
        "generated_at": datetime.now(UTC),
        "data_quality": await data_quality(s, org, site),
        "performance": await performance_report(s, org, site, 7),
        "anomalies": found,
        "maintenance_risk": await maintenance_risk(s, org, site),
        "recommendations": await recommendations(s, org, site),
    }


# --------------------------------------------------------------------------- assistant tools


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class SiteArgs(_Args):
    site_id: uuid.UUID


class SitePeriodArgs(_Args):
    site_id: uuid.UUID
    days: int = Field(default=7, ge=1, le=365)


class PeriodArgs(_Args):
    days: int = Field(default=7, ge=1, le=365)


class AlertArgs(_Args):
    site_id: uuid.UUID | None = None
    include_resolved: bool = False


def build_tools(s: AsyncSession, auth: AuthContext) -> list[Tool]:
    """Read-only tools bound to the caller's organization and permissions."""
    org = auth.org

    def need(permission: P) -> None:
        if permission not in auth.permissions:
            raise ToolError("forbidden", "Your role does not allow access to this information.")

    async def site_of(site_id: uuid.UUID) -> Site:
        site = await s.scalar(select(Site).where(Site.id == site_id, Site.organization_id == org))
        if site is None:
            # Same answer for "does not exist" and "belongs to someone else".
            raise ToolError("not_found", "No such site in your organization.")
        return site

    async def list_sites(_: NoArgs) -> dict[str, Any]:
        need(P.SITES_READ)
        sites = await s.scalars(select(Site).where(Site.organization_id == org).order_by(Site.name))
        return {"sites": [
            {"id": str(x.id), "name": x.name, "kind": x.kind,
             "capacity_kwp": x.installed_capacity_kwp, "simulated": x.is_simulated}
            for x in sites
        ]}  # fmt: skip

    async def get_overview(_: NoArgs) -> dict[str, Any]:
        need(P.TELEMETRY_READ)
        data = await energy.overview(s, org)
        return {k: data[k] for k in (
            "generated_at", "simulated", "site_count", "reporting_sites", "current_power",
            "production_today", "production_month", "battery_soc", "active_alerts")}  # fmt: skip

    async def get_site_energy_summary(args: SitePeriodArgs) -> dict[str, Any]:
        need(P.TELEMETRY_READ)
        site = await site_of(args.site_id)
        start, end = _window(args.days)
        data = await energy.site_summary(s, org, site, start, end)
        return {"site": data.pop("site_name"), **{k: v for k, v in data.items() if k != "site_id"}}

    async def get_live_status(args: SiteArgs) -> dict[str, Any]:
        need(P.TELEMETRY_READ)
        site = await site_of(args.site_id)
        now = datetime.now(UTC)
        fresh_for = timedelta(seconds=site.expected_interval_s * energy.GAP_FACTOR)
        rows = await s.execute(
            text(
                """
                SELECT metric_name,
                       CASE WHEN bool_or(agg = 'sum') THEN sum(value) ELSE avg(value) END AS value,
                       min(time) AS time
                FROM (
                    SELECT DISTINCT ON (device_id, metric_name) metric_name, value, time,
                           CASE WHEN metric_name = ANY(CAST(:summed AS text[]))
                                THEN 'sum' ELSE 'avg' END AS agg
                    FROM telemetry
                    WHERE organization_id = :org AND site_id = :site AND quality = 'valid'
                      AND time >= :since
                    ORDER BY device_id, metric_name, time DESC
                ) latest
                GROUP BY metric_name ORDER BY metric_name
                """
            ),
            {"org": org, "site": site.id, "since": now - timedelta(days=2),
             "summed": [m.name for m in METRICS.values() if m.site_aggregation == "sum"]},
        )  # fmt: skip
        return {"site": site.name, "as_of": now, "simulated": site.is_simulated, "values": [
            {"metric": r.metric_name, "value": round(r.value, 3),
             "unit": METRICS[r.metric_name].unit, "time": r.time,
             "fresh": now - r.time <= fresh_for}
            for r in rows
        ]}  # fmt: skip

    async def list_alerts(args: AlertArgs) -> dict[str, Any]:
        need(P.ALERTS_READ)
        stmt = (
            select(Alert, Site.name)
            .join(Site, Site.id == Alert.site_id)
            .where(Alert.organization_id == org)
            .order_by(Alert.last_seen_at.desc())
        )
        if args.site_id:
            stmt = stmt.where(Alert.site_id == (await site_of(args.site_id)).id)
        if not args.include_resolved:
            stmt = stmt.where(Alert.status != AlertStatus.RESOLVED)
        rows = (await s.execute(stmt.limit(20))).all()
        return {"total": len(rows), "alerts": [
            {"id": str(a.id), "title": a.title, "severity": a.severity, "status": a.status,
             "site": name, "message": a.message, "first_seen_at": a.first_seen_at,
             "last_seen_at": a.last_seen_at, "occurrence_count": a.occurrence_count,
             "evidence": a.evidence}
            for a, name in rows
        ]}  # fmt: skip

    async def compare_sites(args: PeriodArgs) -> dict[str, Any]:
        need(P.TELEMETRY_READ)
        start, end = _window(args.days)
        sites = list(await s.scalars(select(Site).where(Site.organization_id == org)))
        totals: dict[uuid.UUID, dict[str, float]] = {}
        for value in await energy.daily_integrals(s, org, [x.id for x in sites], start, end):
            bucket = totals.setdefault(value.site_id, {})
            bucket[value.metric] = bucket.get(value.metric, 0.0) + value.total
        ranking = performance.compare_sites([
            {"name": x.name, "capacity_kwp": x.installed_capacity_kwp,
             "production_kwh": totals.get(x.id, {}).get("pv_power_kw"),
             "insolation_kwh_m2": totals.get(x.id, {}).get("irradiance_w_m2")}
            for x in sites
        ])  # fmt: skip
        return {"period": {"start": start, "end": end}, "ranking": ranking,
                "simulated": any(x.is_simulated for x in sites)}  # fmt: skip

    async def get_maintenance_risk(args: SiteArgs) -> dict[str, Any]:
        need(P.MAINTENANCE_READ)
        return await maintenance_risk(s, org, await site_of(args.site_id))

    async def get_recommendations(args: SitePeriodArgs) -> dict[str, Any]:
        need(P.TELEMETRY_READ)
        return await recommendations(s, org, await site_of(args.site_id), args.days)

    async def get_data_quality(args: SitePeriodArgs) -> dict[str, Any]:
        need(P.TELEMETRY_READ)
        return await data_quality(s, org, await site_of(args.site_id), min(args.days * 24, 168))

    return [
        Tool("list_sites", "List the solar sites of the user's organization.", NoArgs, list_sites),
        Tool("get_overview", "Organization-wide live figures and active alert counts.",
             NoArgs, get_overview),
        Tool("get_site_energy_summary",
             "Energy KPIs of one site over the last N days (production, consumption, grid, "
             "performance ratio, estimated savings), each tagged measured/calculated/estimated.",
             SitePeriodArgs, get_site_energy_summary),
        Tool("get_live_status", "Latest measurements of one site, with their timestamps.",
             SiteArgs, get_live_status),
        Tool("list_alerts", "Alerts with their supporting evidence, optionally for one site.",
             AlertArgs, list_alerts),
        Tool("compare_sites", "Rank the organization's sites by specific yield over N days.",
             PeriodArgs, compare_sites),
        Tool("get_maintenance_risk",
             "Rule-based maintenance risk of a site with its factors (not a failure prediction).",
             SiteArgs, get_maintenance_risk),
        Tool("get_recommendations", "Optimization recommendations with their assumptions.",
             SitePeriodArgs, get_recommendations),
        Tool("get_data_quality", "Data completeness, gaps and communication health of a site.",
             SitePeriodArgs, get_data_quality),
    ]  # fmt: skip


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai" and settings.openai_api_key is not None:
        return OpenAIProvider(
            api_key=settings.openai_api_key.get_secret_value(), model=settings.openai_model,
            base_url=settings.openai_base_url,
        )  # fmt: skip
    return DeterministicProvider()


async def ask(
    s: AsyncSession, provider: LLMProvider, auth: AuthContext, question: str,
    conversation_id: uuid.UUID | None,
) -> tuple[AIConversation, AIMessage]:  # fmt: skip
    await billing.ensure_feature(s, auth.org, Feature.AI_ASSISTANT)
    if conversation_id is None:
        conversation = AIConversation(
            organization_id=auth.org, user_id=auth.user_id, title=question[:80]
        )
        s.add(conversation)
        await s.flush()
        history: list[dict[str, str]] = []
    else:
        # A conversation is private to its author, even inside the organization.
        found = await s.scalar(
            select(AIConversation).where(
                AIConversation.id == conversation_id,
                AIConversation.organization_id == auth.org,
                AIConversation.user_id == auth.user_id,
            )
        )
        if found is None:
            raise NotFound("Conversation not found")
        conversation = found
        previous = await s.scalars(
            select(AIMessage)
            .where(
                AIMessage.conversation_id == conversation.id, AIMessage.organization_id == auth.org
            )
            .order_by(AIMessage.created_at, AIMessage.id)
        )
        history = [{"role": m.role, "content": m.content} for m in previous]

    answer = await Assistant(provider).answer(question, build_tools(s, auth), history)
    # Both rows are written in one transaction, where now() is constant:
    # explicit timestamps keep the question strictly before its answer.
    asked_at = datetime.now(UTC)
    s.add(AIMessage(organization_id=auth.org, conversation_id=conversation.id, role="user",
                    content=question, created_at=asked_at))  # fmt: skip
    await s.flush()
    reply = AIMessage(
        organization_id=auth.org, conversation_id=conversation.id, role="assistant",
        content=answer.text, provider=answer.provider,
        created_at=asked_at + timedelta(microseconds=1),
        # Which tools were called and with what, for audit. Results are not stored.
        tool_calls=[
            {"name": c["name"], "arguments": _jsonable(c["arguments"]),
             "ok": "error" not in c["result"]}
            for c in answer.tool_calls
        ] + ([{"fallback_reason": answer.fallback_reason}] if answer.fallback_reason else []),
    )  # fmt: skip
    s.add(reply)
    await s.flush()
    await s.refresh(reply)
    audit(s, action="ai.ask", organization_id=auth.org, actor_id=auth.user_id,
          entity_type="ai_conversation", entity_id=conversation.id,
          details={"provider": answer.provider, "tools": [c["name"] for c in answer.tool_calls]})  # fmt: skip
    return conversation, reply


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, uuid.UUID | datetime | date):
        return str(value)
    return value
