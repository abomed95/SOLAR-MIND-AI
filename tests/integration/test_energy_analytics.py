"""Energy KPIs computed from stored telemetry, checked against known fixtures."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from solarmind_adapters import PlantConfig, PlantModel, simulate
from solarmind_energy import Sample, integrate
from tests.helpers import API, create_device, create_gateway, create_site, register

pytestmark = pytest.mark.db

# Yesterday 09:00 local time in Djibouti (UTC+3): safely inside one local day.
START = (datetime.now(UTC) - timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)


@pytest.fixture
async def site(client: AsyncClient):  # type: ignore[no-untyped-def]
    admin = await register(client)
    site = await create_site(client, admin, installed_capacity_kwp=100, tariff_per_kwh="55")
    gateway = await create_gateway(client, admin, site["id"])
    ids = {
        kind: (await create_device(client, admin, site["id"], kind, kind=kind))["id"]
        for kind in ("INVERTER", "METER", "WEATHER_STATION", "BATTERY_BMS")
    }
    return {"admin": admin, "site": site, "key": gateway["api_key"], **ids}


async def ingest(client: AsyncClient, key: str, records: list[dict[str, Any]]) -> None:
    for i in range(0, len(records), 2000):
        response = await client.post(
            f"{API}/ingestion/batch", json={"records": records[i : i + 2000]},
            headers={"X-Gateway-Key": key},
        )  # fmt: skip
        assert response.status_code == 200, response.text
        assert response.json()["rejected"] == []


def at(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


async def summary(client: AsyncClient, site: dict, start: datetime, end: datetime) -> dict:  # type: ignore[type-arg]
    response = await client.get(
        f"{API}/energy/sites/{site['site']['id']}/summary",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers=site["admin"].headers,
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_kpis_for_a_known_two_hour_profile(client: AsyncClient, site: dict) -> None:  # type: ignore[type-arg]
    """10 kW PV, 4 kW load, 6 kW export, 500 W/m2, for exactly two hours."""
    records = []
    for m in range(0, 121, 5):
        records += [
            {"device_id": site["INVERTER"], "timestamp": at(m), "metrics": {"pv_power_kw": 10.0}},
            {"device_id": site["METER"], "timestamp": at(m),
             "metrics": {"load_power_kw": 4.0, "grid_export_kw": 6.0, "grid_import_kw": 0.0}},
            {"device_id": site["WEATHER_STATION"], "timestamp": at(m),
             "metrics": {"irradiance_w_m2": 500.0}},
            {"device_id": site["BATTERY_BMS"], "timestamp": at(m),
             "metrics": {"battery_soc_percent": 50.0 + m / 10}},
        ]  # fmt: skip
    await ingest(client, site["key"], records)

    k = await summary(client, site, START, START + timedelta(minutes=121))
    assert k["production"] == {"value": 20.0, "unit": "kWh", "kind": "calculated",
                               "coverage": pytest.approx(7200 / 7260, abs=1e-4)}  # fmt: skip
    assert k["consumption"]["value"] == 8.0
    assert k["grid_export"]["value"] == 12.0
    assert k["grid_import"]["value"] == 0.0
    # 20 kWh produced - 12 exported = 8 used on site; all 8 kWh of load covered by PV.
    assert k["self_consumption"]["value"] == 8.0
    assert k["self_consumption"]["ratio"] == pytest.approx(0.4)
    assert k["self_sufficiency"]["value"] == 1.0
    # 500 W/m2 x 2 h = 1 kWh/m2; PR = (20 / 100) / 1 = 0.2
    assert k["performance_ratio"]["insolation_kwh_m2"] == 1.0
    assert k["performance_ratio"]["value"] == pytest.approx(0.2)
    assert k["specific_yield"]["value"] == pytest.approx(0.2)
    # 8 kWh x 55 DJF, whole francs, labelled as an estimate with its assumptions.
    savings = k["estimated_savings"]
    assert (savings["value"], savings["unit"], savings["kind"]) == (440, "DJF", "estimated")
    assert len(savings["assumptions"]) == 3
    assert k["availability"]["value"] == 1.0
    assert k["battery_soc"] == {"min": 50.0, "avg": 56.0, "max": 62.0, "unit": "%",
                                "kind": "measured"}  # fmt: skip
    assert k["data_quality"]["valid"] == 25 * 6 and k["data_quality"]["out_of_range"] == 0

    daily = await client.get(
        f"{API}/energy/sites/{site['site']['id']}/daily",
        params={"start": (START - timedelta(days=1)).isoformat(),
                "end": (START + timedelta(days=1)).isoformat()},
        headers=site["admin"].headers,
    )  # fmt: skip
    assert daily.json() == [{
        "day": (START + timedelta(hours=3)).date().isoformat(), "kind": "calculated",
        "production_kwh": 20.0, "consumption_kwh": 8.0, "grid_export_kwh": 12.0,
        "grid_import_kwh": 0.0, "insolation_kwh_m2": 1.0,
        "coverage": pytest.approx(7200 / 86400, abs=1e-4),
    }]  # fmt: skip


async def test_a_data_gap_carries_no_energy(client: AsyncClient, site: dict) -> None:  # type: ignore[type-arg]
    """30 min at 10 kW, two hours without data, 30 min at 10 kW."""
    minutes = list(range(0, 31, 5)) + list(range(150, 181, 5))
    await ingest(client, site["key"], [
        {"device_id": site["INVERTER"], "timestamp": at(m), "metrics": {"pv_power_kw": 10.0}}
        for m in minutes
    ])  # fmt: skip
    k = await summary(client, site, START, START + timedelta(minutes=181))
    # 10 kWh, not the 30 kWh a naive interpolation across the hole would give.
    assert k["production"]["value"] == 10.0
    assert k["production"]["coverage"] == pytest.approx(3600 / (181 * 60), abs=1e-3)
    assert k["availability"]["value"] == pytest.approx(14 / 36, abs=0.01)


async def test_flagged_values_are_excluded_from_energy(client: AsyncClient, site: dict) -> None:  # type: ignore[type-arg]
    records = [
        {"device_id": site["INVERTER"], "timestamp": at(m), "metrics": {"pv_power_kw": 10.0}}
        for m in range(0, 61, 5)
    ]
    records[6]["metrics"] = {"pv_power_kw": 9e9}  # physically impossible spike
    await ingest(client, site["key"], records)
    k = await summary(client, site, START, START + timedelta(minutes=61))
    assert k["production"]["value"] == 10.0  # the spike is bridged, not integrated
    assert k["data_quality"]["out_of_range"] == 1


async def test_no_data_is_reported_as_unknown_not_zero(client: AsyncClient, site: dict) -> None:  # type: ignore[type-arg]
    k = await summary(client, site, START, START + timedelta(hours=2))
    assert k["production"] == {"value": None, "unit": "kWh", "kind": "calculated", "coverage": 0.0}
    assert k["performance_ratio"]["value"] is None
    assert k["estimated_savings"]["value"] is None
    assert k["availability"]["value"] == 0.0

    overview = (await client.get(f"{API}/energy/overview", headers=site["admin"].headers)).json()
    assert overview["current_power"]["value"] is None
    assert overview["production_today"]["value"] is None
    assert overview["total_production"]["value"] is None
    assert overview["sites"][0]["reporting"] is False


async def test_missing_reference_data_is_explained(client: AsyncClient, site: dict) -> None:  # type: ignore[type-arg]
    await ingest(client, site["key"], [
        {"device_id": site["INVERTER"], "timestamp": at(m), "metrics": {"pv_power_kw": 10.0}}
        for m in range(0, 61, 5)
    ])  # fmt: skip
    k = await summary(client, site, START, START + timedelta(minutes=61))
    assert k["performance_ratio"]["value"] is None
    assert "irradiance" in k["performance_ratio"]["note"]
    assert "assumed" in k["self_consumption"]["note"]


async def test_sql_integration_agrees_with_the_reference_engine(
    client: AsyncClient,
    site: dict,  # type: ignore[type-arg]
) -> None:
    """Same samples through the API (SQL) and through solarmind_energy."""
    day_start = START - timedelta(hours=9)  # yesterday 00:00 local
    states = simulate(PlantModel(PlantConfig(seed=11, weather="partly_cloudy")), day_start,
                      day_start + timedelta(days=1), timedelta(minutes=5))  # fmt: skip
    # Drop two hours of the afternoon to make sure the gap rule is exercised.
    kept = [s for s in states if not 13 <= (s.at - day_start).total_seconds() / 3600 < 15]
    await ingest(client, site["key"], [
        {"device_id": site["INVERTER"], "timestamp": s.at.isoformat(),
         "metrics": {"pv_power_kw": s.values["pv_power_kw"]}}
        for s in kept
    ])  # fmt: skip
    reference = integrate([Sample(s.at, s.values["pv_power_kw"]) for s in kept], max_gap_s=3 * 300)
    k = await summary(client, site, day_start, day_start + timedelta(days=1))
    assert reference.total > 100
    assert k["production"]["value"] == pytest.approx(reference.total, abs=0.001)
    assert k["production"]["coverage"] == pytest.approx(reference.covered_s / 86400, abs=1e-4)


async def test_overview_live_values_and_equipment_health(client: AsyncClient, site: dict) -> None:  # type: ignore[type-arg]
    now = datetime.now(UTC).replace(microsecond=0)
    records = []
    for minutes_ago in range(25, -1, -5):
        stamp = (now - timedelta(minutes=minutes_ago)).isoformat()
        records += [
            {"device_id": site["INVERTER"], "timestamp": stamp,
             "metrics": {"pv_power_kw": 40.0, "total_energy_kwh": 123456.0}},
            {"device_id": site["METER"], "timestamp": stamp, "metrics": {"load_power_kw": 15.0}},
            {"device_id": site["BATTERY_BMS"], "timestamp": stamp,
             "metrics": {"battery_soc_percent": 80.0}},
        ]  # fmt: skip
    # The weather station last reported hours ago: stale, so not "current".
    records.append({"device_id": site["WEATHER_STATION"], "metrics": {"irradiance_w_m2": 700.0},
                    "timestamp": (now - timedelta(hours=5)).isoformat()})  # fmt: skip
    await ingest(client, site["key"], records)
    admin = site["admin"]

    o = (await client.get(f"{API}/energy/overview", headers=admin.headers)).json()
    assert o["current_power"] == {"value": 40.0, "unit": "kW", "kind": "measured"}
    assert o["current_consumption"]["value"] == 15.0
    assert o["battery_soc"]["value"] == 80.0
    assert o["total_production"]["value"] == 123456.0
    assert o["production_today"]["kind"] == "calculated"
    assert o["estimated_savings_month"]["kind"] == "estimated"
    assert (o["site_count"], o["reporting_sites"], o["simulated"]) == (1, 1, False)
    assert o["active_alerts"] == {"CRITICAL": 0, "HIGH": 0, "WARNING": 0, "INFO": 0}
    assert o["sites"][0]["current_power_kw"] == 40.0

    health = (await client.get(f"{API}/energy/equipment-health", headers=admin.headers)).json()
    status = {h["kind"]: h["status"] for h in health}
    assert status == {"INVERTER": "ONLINE", "METER": "ONLINE", "BATTERY_BMS": "ONLINE",
                      "WEATHER_STATION": "OFFLINE"}  # fmt: skip
    inverter = next(h for h in health if h["kind"] == "INVERTER")
    assert 0 < inverter["availability_24h"] < 0.05  # 6 of 288 slots


async def test_analytics_are_tenant_scoped_and_permissioned(
    client: AsyncClient, site: dict
) -> None:  # type: ignore[type-arg]
    intruder = await register(client, "Intruder")
    window = {"start": START.isoformat(), "end": (START + timedelta(hours=1)).isoformat()}
    for path in ("summary", "daily"):
        response = await client.get(
            f"{API}/energy/sites/{site['site']['id']}/{path}", params=window,
            headers=intruder.headers,
        )  # fmt: skip
        assert response.status_code == 404
    assert (await client.get(f"{API}/energy/overview", headers=intruder.headers)).json()[
        "sites"
    ] == []
    assert (
        await client.get(f"{API}/energy/equipment-health", headers=intruder.headers)
    ).json() == []
    client.cookies.clear()  # registering left a session cookie on this client
    assert (await client.get(f"{API}/energy/overview")).status_code == 401

    bad = await client.get(
        f"{API}/energy/sites/{site['site']['id']}/summary",
        params={"start": START.isoformat(), "end": (START - timedelta(hours=1)).isoformat()},
        headers=site["admin"].headers,
    )
    assert bad.status_code == 422
