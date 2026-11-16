"""Telemetry ingestion end to end: REST, MQTT, idempotency, offline replay."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from solarmind_adapters import (
    GatewayRunner,
    MockSolarAdapter,
    MqttTransport,
    Outbox,
    PlantConfig,
    PlantModel,
    RestTransport,
)
from solarmind_api.services import ingestion
from solarmind_schemas import TelemetryRecord
from tests.helpers import API, create_device, create_gateway, create_site, register

pytestmark = pytest.mark.db

NOW = datetime.now(UTC).replace(microsecond=0)


def rec(device_id: str, minutes_ago: int, **metrics: float | None) -> dict[str, Any]:
    return {"device_id": device_id, "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "metrics": metrics}  # fmt: skip


async def post(
    client: AsyncClient, key: str | None, records: list[dict[str, Any]]
) -> httpx.Response:
    headers = {"X-Gateway-Key": key} if key else {}
    return await client.post(f"{API}/ingestion/batch", json={"records": records}, headers=headers)


@pytest.fixture
async def plant(client: AsyncClient):  # type: ignore[no-untyped-def]
    admin = await register(client)
    site = await create_site(client, admin)
    gateway = await create_gateway(client, admin, site["id"])
    inverter = await create_device(client, admin, site["id"], "Inverter", gateway_id=gateway["id"])
    meter = await create_device(client, admin, site["id"], "Meter", kind="METER")
    return {"admin": admin, "site": site, "gateway": gateway, "key": gateway["api_key"],
            "inverter": inverter["id"], "meter": meter["id"]}  # fmt: skip


def count_rows(owner_dsn: str, where: str = "true") -> int:
    with psycopg.connect(owner_dsn) as conn:
        return conn.execute(f"SELECT count(*) FROM telemetry WHERE {where}").fetchone()[0]  # type: ignore[index]  # noqa: S608


async def test_batch_is_stored_and_replay_is_idempotent(
    client: AsyncClient,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    records = [
        rec(plant["inverter"], 10, pv_power_kw=12.5, inverter_temperature_c=42.5),
        rec(plant["inverter"], 5, pv_power_kw=13.0, inverter_temperature_c=43.0),
        rec(plant["meter"], 5, load_power_kw=8.0),
    ]
    first = await post(client, plant["key"], records)
    assert first.status_code == 200
    assert first.json() == {"received": 5, "inserted": 5, "duplicates": 0, "out_of_range": 0,
                            "rejected": []}  # fmt: skip
    again = await post(client, plant["key"], records)
    assert again.json()["inserted"] == 0 and again.json()["duplicates"] == 5
    assert count_rows(owner_dsn) == 5

    # A duplicate inside one batch is also stored once.
    twice = await post(client, plant["key"], [rec(plant["meter"], 1, load_power_kw=9.0)] * 2)
    assert twice.json()["inserted"] == 1 and twice.json()["duplicates"] == 1


async def test_a_replayed_value_never_overwrites_the_original(
    client: AsyncClient,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    await post(client, plant["key"], [rec(plant["inverter"], 5, pv_power_kw=12.5)])
    await post(client, plant["key"], [rec(plant["inverter"], 5, pv_power_kw=99.0)])
    with psycopg.connect(owner_dsn) as conn:
        assert conn.execute("SELECT value FROM telemetry").fetchall() == [(12.5,)]


async def test_invalid_input_is_rejected_per_record(
    client: AsyncClient,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    future = {"device_id": plant["inverter"], "metrics": {"pv_power_kw": 1.0},
              "timestamp": (NOW + timedelta(hours=2)).isoformat()}  # fmt: skip
    ancient = {"device_id": plant["inverter"], "metrics": {"pv_power_kw": 1.0},
               "timestamp": "2001-01-01T00:00:00Z"}  # fmt: skip
    response = await post(client, plant["key"], [
        rec(str(uuid.uuid4()), 5, pv_power_kw=1.0),                   # 0 unknown device
        future,                                                        # 1
        ancient,                                                       # 2
        rec(plant["inverter"], 5, pv_power_kw=10.0, flux_capacitor=1.21),  # 3 one bad metric
        rec(plant["inverter"], 4, pv_power_kw=None, inverter_temperature_c=40.0),  # 4 missing value
        rec(plant["inverter"], 3, battery_soc_percent=140.0),          # 5 physically impossible
    ])  # fmt: skip
    body = response.json()
    assert response.status_code == 200
    assert body["rejected"] == [
        {"index": 0, "reason": "unknown_device"},
        {"index": 1, "reason": "timestamp_in_future"},
        {"index": 2, "reason": "timestamp_too_old"},
        {"index": 3, "reason": "unknown_metric", "metric": "flux_capacitor"},
    ]
    assert body["inserted"] == 3 and body["out_of_range"] == 1
    with psycopg.connect(owner_dsn) as conn:
        rows = conn.execute(
            "SELECT metric_name, value, quality FROM telemetry ORDER BY time, metric_name"
        ).fetchall()
    # The missing pv_power_kw was not stored as zero; the impossible SOC is
    # kept exactly as received, flagged.
    assert rows == [("pv_power_kw", 10.0, "valid"), ("inverter_temperature_c", 40.0, "valid"),
                    ("battery_soc_percent", 140.0, "out_of_range")]  # fmt: skip


@pytest.mark.parametrize(
    "payload",
    [
        {"records": []},
        {
            "records": [
                {
                    "device_id": "not-a-uuid",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "metrics": {"a": 1},
                }
            ]
        },
        {
            "records": [
                {
                    "device_id": str(uuid.uuid4()),
                    "timestamp": "2026-01-01T00:00:00",
                    "metrics": {"a": 1},
                }
            ]
        },
        {
            "records": [
                {"device_id": str(uuid.uuid4()), "timestamp": "2026-01-01T00:00:00Z", "metrics": {}}
            ]
        },
        {
            "records": [
                {
                    "device_id": str(uuid.uuid4()),
                    "timestamp": "2026-01-01T00:00:00Z",
                    "metrics": {"a": "x"},
                }
            ]
        },
        {
            "records": [
                {
                    "device_id": str(uuid.uuid4()),
                    "timestamp": "2026-01-01T00:00:00Z",
                    "metrics": {"a": 1},
                    "organization_id": "x",
                }
            ]
        },
    ],
)
async def test_malformed_batches_are_refused(
    client: AsyncClient, plant: dict, payload: dict
) -> None:  # type: ignore[type-arg]
    response = await client.post(
        f"{API}/ingestion/batch", json=payload, headers={"X-Gateway-Key": plant["key"]}
    )
    assert response.status_code == 422


async def test_non_finite_numbers_are_refused(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    body = json.dumps({"records": [rec(plant["inverter"], 1, pv_power_kw=1.0)]}).replace(
        "1.0", "NaN"
    )
    response = await client.post(
        f"{API}/ingestion/batch", content=body,
        headers={"X-Gateway-Key": plant["key"], "Content-Type": "application/json"},
    )  # fmt: skip
    assert response.status_code == 422


async def test_gateway_authentication(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    records = [rec(plant["inverter"], 1, pv_power_kw=1.0)]
    assert (await post(client, None, records)).status_code == 401
    assert (await post(client, "wrong-key", records)).status_code == 401
    # A user session token is not a gateway key.
    assert (await post(client, plant["admin"].token, records)).status_code == 401
    # After rotation the old key stops working.
    rotated = await client.post(
        f"{API}/gateways/{plant['gateway']['id']}/rotate-key", headers=plant["admin"].headers
    )
    assert (await post(client, plant["key"], records)).status_code == 401
    assert (await post(client, rotated.json()["api_key"], records)).status_code == 200


async def test_gateway_cannot_write_to_another_organization_or_site(
    client: AsyncClient,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    other = await register(client, "Other Org")
    other_site = await create_site(client, other)
    other_device = await create_device(client, other, other_site["id"])
    # Same organization, different site.
    second_site = await create_site(client, plant["admin"], "Second site")
    sibling = await create_device(client, plant["admin"], second_site["id"])

    response = await post(client, plant["key"], [
        rec(other_device["id"], 1, pv_power_kw=5.0),
        rec(sibling["id"], 1, pv_power_kw=5.0),
    ])  # fmt: skip
    assert [r["reason"] for r in response.json()["rejected"]] == ["unknown_device"] * 2
    assert count_rows(owner_dsn) == 0


async def test_simulated_devices_can_never_report_measured_data(
    client: AsyncClient,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute("UPDATE devices SET is_simulated = true WHERE id = %s", (plant["inverter"],))
    record = {**rec(plant["inverter"], 1, pv_power_kw=5.0), "source": "MEASURED"}
    await post(client, plant["key"], [record, rec(plant["meter"], 1, load_power_kw=2.0)])
    with psycopg.connect(owner_dsn) as conn:
        rows = dict(conn.execute("SELECT metric_name, source FROM telemetry").fetchall())
    assert rows == {"pv_power_kw": "SIMULATED", "load_power_kw": "MEASURED"}


async def test_liveness_and_queries(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    admin = plant["admin"]
    records = [rec(plant["inverter"], m, pv_power_kw=10.0 + m) for m in (20, 15, 10, 5)]
    records.append(rec(plant["meter"], 5, load_power_kw=4.0))
    response = await client.post(
        f"{API}/ingestion/batch", json={"records": records, "buffered_records": 7},
        headers={"X-Gateway-Key": plant["key"]},
    )  # fmt: skip
    assert response.status_code == 200

    device = (await client.get(f"{API}/devices/{plant['inverter']}", headers=admin.headers)).json()
    assert device["status"] == "ONLINE"
    assert device["last_seen_at"].startswith((NOW - timedelta(minutes=5)).isoformat()[:19])
    gateway = (await client.get(f"{API}/gateways", headers=admin.headers)).json()[0]
    assert gateway["status"] == "ONLINE" and gateway["buffered_records"] == 7

    latest = (await client.get(f"{API}/telemetry/latest", headers=admin.headers)).json()
    assert {(r["metric_name"], r["value"], r["unit"]) for r in latest} == {
        ("pv_power_kw", 15.0, "kW"), ("load_power_kw", 4.0, "kW")}  # fmt: skip

    window = {
        "start": (NOW - timedelta(hours=1)).isoformat(),
        "end": (NOW + timedelta(minutes=1)).isoformat(),
    }
    series = (
        await client.get(
            f"{API}/telemetry/series",
            params={"site_id": plant["site"]["id"], "metric": "pv_power_kw", **window},
            headers=admin.headers,
        )
    ).json()
    assert series["unit"] == "kW" and series["bucket_seconds"] == 300
    assert sorted(p["value"] for p in series["points"]) == [15.0, 20.0, 25.0, 30.0]
    # Only buckets that hold data are returned: gaps stay gaps.
    assert len(series["points"]) == 4

    raw = (
        await client.get(
            f"{API}/telemetry/raw", params={"device_id": plant["inverter"], **window},
            headers=admin.headers,
        )
    ).json()  # fmt: skip
    assert len(raw) == 4 and {r["quality"] for r in raw} == {"valid"}

    bad = await client.get(
        f"{API}/telemetry/series",
        params={"site_id": plant["site"]["id"], "metric": "'; DROP TABLE telemetry;--", **window},
        headers=admin.headers,
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_metric"


async def test_hourly_aggregate_matches_raw_data(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    admin = plant["admin"]
    base = (NOW - timedelta(days=2)).replace(minute=0, second=0)
    records = [
        {
            "device_id": plant["inverter"],
            "timestamp": (base + timedelta(minutes=m)).isoformat(),
            "metrics": {"pv_power_kw": float(v)},
        }  # fmt: skip
        for m, v in ((0, 10), (20, 20), (40, 30), (60, 100))
    ]
    await post(client, plant["key"], records)
    series = (
        await client.get(
            f"{API}/telemetry/series",
            params={"site_id": plant["site"]["id"], "metric": "pv_power_kw", "bucket_seconds": 3600,
                    "start": (base - timedelta(hours=1)).isoformat(),
                    "end": (base + timedelta(hours=3)).isoformat()},
            headers=admin.headers,
        )
    ).json()  # fmt: skip
    assert [(p["value"], p["samples"], p["min"], p["max"]) for p in series["points"]] == [
        (20.0, 3, 10.0, 30.0), (100.0, 1, 100.0, 100.0)]  # fmt: skip


async def test_telemetry_is_isolated_between_organizations(
    client: AsyncClient,
    app: FastAPI,
    plant: dict,  # type: ignore[type-arg]
) -> None:
    await post(client, plant["key"], [rec(plant["inverter"], 5, pv_power_kw=12.5)])
    await post(client, plant["key"], [
        {"device_id": plant["inverter"], "timestamp": (NOW - timedelta(days=5)).isoformat(),
         "metrics": {"pv_power_kw": 7.0}}])  # fmt: skip
    intruder = await register(client, "Intruder")
    window = {"start": (NOW - timedelta(days=10)).isoformat(), "end": NOW.isoformat()}

    assert (await client.get(f"{API}/telemetry/latest", headers=intruder.headers)).json() == []
    filtered = await client.get(
        f"{API}/telemetry/latest", params={"site_id": plant["site"]["id"]}, headers=intruder.headers
    )
    assert filtered.json() == []
    series = await client.get(
        f"{API}/telemetry/series",
        params={"site_id": plant["site"]["id"], "metric": "pv_power_kw", **window},
        headers=intruder.headers,
    )
    assert series.status_code == 404
    raw = await client.get(
        f"{API}/telemetry/raw", params={"device_id": plant["inverter"], **window},
        headers=intruder.headers,
    )  # fmt: skip
    assert raw.status_code == 404

    # RLS on the hypertable and on the aggregate view, with no application filter.
    async with app.state.db.tenant(uuid.UUID(intruder.org_id)) as s:
        assert await s.scalar(text("SELECT count(*) FROM telemetry")) == 0
        assert await s.scalar(text("SELECT count(*) FROM telemetry_hourly_tenant")) == 0
    async with app.state.db.tenant(uuid.UUID(plant["admin"].org_id)) as s:
        assert await s.scalar(text("SELECT count(*) FROM telemetry")) == 2
        assert await s.scalar(text("SELECT count(*) FROM telemetry_hourly_tenant")) == 2


# --------------------------------------------------------------------------- offline replay


class SwitchableNetwork(httpx.AsyncBaseTransport):
    """Wraps the in-process API and can simulate a network outage."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = ASGITransport(app=app)
        self.online = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("network unreachable")
        return await self._inner.handle_async_request(request)


async def test_disconnected_gateway_synchronizes_after_reconnection(
    client: AsyncClient,
    app: FastAPI,
    plant: dict,
    owner_dsn: str,
    tmp_path: Path,  # type: ignore[type-arg]
) -> None:
    start = NOW - timedelta(hours=2)
    clock = [start]
    model = PlantModel(PlantConfig(seed=3))
    adapters = [
        MockSolarAdapter(uuid.UUID(plant["inverter"]), model, "INVERTER", clock=lambda: clock[0]),
        MockSolarAdapter(uuid.UUID(plant["meter"]), model, "METER", clock=lambda: clock[0]),
    ]
    network = SwitchableNetwork(app)
    http = httpx.AsyncClient(transport=network, base_url="http://test")
    outbox = Outbox(tmp_path / "gateway.sqlite3")
    runner = GatewayRunner(adapters, outbox, RestTransport(http, plant["key"]), batch_size=10)

    async def cycle() -> None:
        await runner.poll_once()
        await runner.flush()
        clock[0] += timedelta(minutes=5)

    for _ in range(3):  # online: 3 cycles x 7 metric values
        await cycle()
    assert count_rows(owner_dsn) == 21 and outbox.count() == 0

    network.online = False
    for _ in range(12):  # one hour without connectivity
        await cycle()
    assert count_rows(owner_dsn) == 21  # nothing reached the platform
    assert outbox.count() == 24  # 12 cycles x 2 devices, kept on the gateway

    network.online = True
    await cycle()  # reconnection: backlog and the new reading are delivered
    assert outbox.count() == 0
    assert count_rows(owner_dsn) == 16 * 7
    with psycopg.connect(owner_dsn) as conn:
        timestamps = conn.execute(
            "SELECT count(DISTINCT time), min(time), max(time) FROM telemetry "
            "WHERE metric_name = 'pv_power_kw'"
        ).fetchone()
    # Every 5-minute reading of the outage is present, with its original timestamp.
    assert timestamps == (16, start, start + timedelta(minutes=75))
    await http.aclose()


# --------------------------------------------------------------------------- MQTT


def _mqtt_body(plant: dict, key: str) -> bytes:  # type: ignore[type-arg]
    return json.dumps(
        {"api_key": key, "records": [rec(plant["inverter"], 5, pv_power_kw=12.5)]}
    ).encode()


async def test_mqtt_message_handling(app: FastAPI, plant: dict, owner_dsn: str) -> None:  # type: ignore[type-arg]
    db, settings = app.state.db, app.state.settings
    topic = f"solarmind/v1/{plant['gateway']['id']}/telemetry"

    result = await ingestion.handle_mqtt_message(
        db, settings, topic, _mqtt_body(plant, plant["key"])
    )
    assert result is not None and result.inserted == 1
    # Rejected without raising: wrong key, key not matching the topic, garbage.
    assert (
        await ingestion.handle_mqtt_message(db, settings, topic, _mqtt_body(plant, "bad")) is None
    )
    foreign_topic = f"solarmind/v1/{uuid.uuid4()}/telemetry"
    assert await ingestion.handle_mqtt_message(
        db, settings, foreign_topic, _mqtt_body(plant, plant["key"])
    ) is None  # fmt: skip
    for garbage in (b"not json", b"[]", b'{"records": "x"}', b"\xff\xfe"):
        assert await ingestion.handle_mqtt_message(db, settings, topic, garbage) is None
    assert count_rows(owner_dsn) == 1


async def test_mqtt_end_to_end_through_a_real_broker(
    app: FastAPI,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    """Gateway -> Mosquitto -> worker consumer -> database."""
    from pydantic import SecretStr

    from solarmind_api.worker import mqtt_consumer

    host, port = "127.0.0.1", int(os.environ.get("MQTT_PORT", "1883"))
    settings = app.state.settings.model_copy(update={
        "mqtt_host": host, "mqtt_port": port, "mqtt_username": "worker",
        "mqtt_password": SecretStr(os.environ["MQTT_WORKER_PASSWORD"]),
    })  # fmt: skip
    stop = asyncio.Event()
    consumer = asyncio.create_task(mqtt_consumer(app.state.db, settings, stop))
    await asyncio.sleep(1.0)  # let the subscription settle

    transport = MqttTransport(
        host=host, port=port, username="gateway", password=os.environ["MQTT_GATEWAY_PASSWORD"],
        gateway_id=uuid.UUID(plant["gateway"]["id"]), api_key=plant["key"],
    )  # fmt: skip
    records = [TelemetryRecord.model_validate(rec(plant["inverter"], m, pv_power_kw=float(m)))
               for m in (15, 10, 5)]  # fmt: skip
    await transport.send(records, 0)
    await transport.send(records, 0)  # replay: must not duplicate

    for _ in range(50):
        if count_rows(owner_dsn) >= 3:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    stop.set()
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    assert count_rows(owner_dsn) == 3
