"""Solar plant simulator.

Two commands, both using only the platform's public API:

``seed``  registers demo organizations, sites, devices and gateways, then
          back-fills simulated history. Writes the gateway configuration.
``run``   plays the gateways live: polls the simulated plants, buffers in
          SQLite, forwards over REST or MQTT.

All data is SIMULATED and labelled as such end to end.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from solarmind_adapters import (
    GatewayRunner,
    MockSolarAdapter,
    MqttTransport,
    Outbox,
    PlantConfig,
    PlantModel,
    RestTransport,
    Transport,
)
from solarmind_schemas import Source
from solarmind_simulator.plants import (
    DEMO_ORGANIZATIONS,
    plant_config_from_json,
    plant_config_to_json,
    roles_for,
)

logger = logging.getLogger("solarmind_simulator")
ROLE_METRICS = {
    "INVERTER": ("pv_power_kw", "daily_energy_kwh", "total_energy_kwh", "inverter_temperature_c"),
    "BATTERY_BMS": ("battery_soc_percent", "battery_voltage_v", "battery_power_kw",
                    "battery_temperature_c"),
    "METER": ("load_power_kw", "grid_import_kw", "grid_export_kw"),
    "WEATHER_STATION": ("irradiance_w_m2", "ambient_temperature_c"),
}  # fmt: skip


def floor_to(at: datetime, interval_s: int) -> datetime:
    """Align to the sampling grid so that live data continues the history."""
    return datetime.fromtimestamp(int(at.timestamp()) // interval_s * interval_s, tz=UTC)


async def _post(client: httpx.AsyncClient, path: str, body: dict[str, Any], **kw: Any) -> Any:
    response = await client.post(f"/api/v1{path}", json=body, **kw)
    if response.status_code >= 400:
        raise SystemExit(f"POST {path} failed: {response.status_code} {response.text[:300]}")
    return response.json()


async def seed(api_url: str, out: Path, days: int, interval_s: int, password: str,
               transport: httpx.AsyncBaseTransport | None = None) -> None:  # fmt: skip
    if out.exists():
        logger.info("simulator configuration already present, seeding skipped: %s", out)
        return
    now = floor_to(datetime.now(UTC), interval_s)
    history_start = now - timedelta(days=days)
    plants: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=api_url, timeout=60, transport=transport) as client:
        for org_index, org in enumerate(DEMO_ORGANIZATIONS):
            session = await _post(client, "/auth/register", {
                "organization_name": org.name, "full_name": "Demo Administrator",
                "email": org.admin_email, "password": password,
            })  # fmt: skip
            auth = {"Authorization": f"Bearer {session['token']}"}
            for site_index, spec in enumerate(org.sites):
                site = await _post(client, "/sites", {
                    "name": spec.name, "kind": spec.kind, "is_simulated": True,
                    "installed_capacity_kwp": spec.capacity_kwp,
                    "battery_capacity_kwh": spec.battery_kwh or None,
                    "latitude": spec.latitude, "longitude": spec.longitude,
                    "tariff_per_kwh": spec.tariff_per_kwh, "expected_interval_s": interval_s,
                }, headers=auth)  # fmt: skip
                gateway = await _post(client, "/gateways", {
                    "site_id": site["id"], "name": f"{spec.name} gateway"}, headers=auth)  # fmt: skip
                devices = []
                for role in roles_for(spec):
                    device = await _post(client, "/devices", {
                        "site_id": site["id"], "name": f"{spec.name} {role.replace('_', ' ').lower()}",
                        "kind": role, "protocol": "MOCK", "gateway_id": gateway["id"],
                        "is_simulated": True,
                    }, headers=auth)  # fmt: skip
                    devices.append({"id": device["id"], "role": role})
                config = PlantConfig(
                    capacity_kwp=spec.capacity_kwp,
                    battery_kwh=spec.battery_kwh,
                    base_load_kw=spec.base_load_kw,
                    latitude=spec.latitude,
                    weather=spec.weather,  # type: ignore[arg-type]
                    seed=1000 * (org_index + 1) + site_index,
                    faults=[f.resolve(now, "Africa/Djibouti") for f in spec.faults],
                )
                plant = {"gateway_id": gateway["id"], "api_key": gateway["api_key"],
                         "site": spec.name, "devices": devices,
                         "history_start": history_start.isoformat(),
                         "plant": plant_config_to_json(config)}  # fmt: skip
                await backfill(client, plant, history_start, now, interval_s)
                plants.append(plant)
                logger.info("seeded %s / %s (%d days of history)", org.name, spec.name, days)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"interval_s": interval_s, "plants": plants}, indent=2))
    out.chmod(0o600)  # contains gateway keys
    logger.info("simulator configuration written to %s", out)


async def backfill(client: httpx.AsyncClient, plant: dict[str, Any], start: datetime,
                   end: datetime, interval_s: int) -> None:  # fmt: skip
    """Replay history through the same ingestion endpoint a gateway uses."""
    from solarmind_adapters import FaultKind

    model = PlantModel(plant_config_from_json(plant["plant"]))
    records: list[dict[str, Any]] = []
    at = start
    while at < end:
        state = model.state_at(at)
        if not model.has_fault(FaultKind.COMM_LOSS, at):
            missing = model.has_fault(FaultKind.MISSING_METRICS, at)
            for device in plant["devices"]:
                names = ROLE_METRICS[device["role"]]
                if missing:
                    names = names[::2]
                records.append({"device_id": device["id"], "timestamp": at.isoformat(),
                                "metrics": {n: state.values[n] for n in names},
                                "source": Source.SIMULATED})  # fmt: skip
        at += timedelta(seconds=interval_s)
        if len(records) >= 2000 or at >= end:
            if records:
                await _post(client, "/ingestion/batch", {"records": records},
                            headers={"X-Gateway-Key": plant["api_key"]})  # fmt: skip
            records = []


async def run(config_path: Path, api_url: str, state_dir: Path, transport_kind: str) -> None:
    config = json.loads(config_path.read_text())
    interval_s = int(config["interval_s"])
    state_dir.mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def clock() -> datetime:
        return floor_to(datetime.now(UTC), interval_s)

    client = httpx.AsyncClient(base_url=api_url, timeout=30)
    runners = []
    for plant in config["plants"]:
        model = PlantModel(plant_config_from_json(plant["plant"]))
        # Warm up through the seeded history (not emitted) so that battery
        # state and energy counters continue exactly where the back-fill ended.
        at, end = datetime.fromisoformat(plant["history_start"]), clock()
        while at < end:
            model.state_at(at)
            at += timedelta(seconds=interval_s)
        adapters = [
            MockSolarAdapter(uuid.UUID(d["id"]), model, d["role"], clock=clock)
            for d in plant["devices"]
        ]
        transport: Transport
        if transport_kind == "mqtt":
            transport = MqttTransport(
                host=os.environ["SIMULATOR_MQTT_HOST"],
                port=int(os.environ.get("SIMULATOR_MQTT_PORT", "1883")),
                username="gateway", password=os.environ["SIMULATOR_MQTT_PASSWORD"],
                gateway_id=uuid.UUID(plant["gateway_id"]), api_key=plant["api_key"],
            )  # fmt: skip
        else:
            transport = RestTransport(client, plant["api_key"])
        outbox = Outbox(state_dir / f"{plant['gateway_id']}.sqlite3")
        runners.append(GatewayRunner(adapters, outbox, transport))
    logger.info("simulating %d plants every %d s over %s", len(runners), interval_s, transport_kind)
    # Poll slightly faster than the sampling grid; identical timestamps are
    # deduplicated by the platform, so no reading is missed or doubled.
    await asyncio.gather(*(r.run(interval_s / 2, stop) for r in runners))
    await client.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="solarmind_simulator")
    parser.add_argument("command", choices=["seed", "run"])
    parser.add_argument("--api-url", default=os.environ.get("SIMULATOR_API_URL", "http://api:8000"))
    parser.add_argument("--state-dir", type=Path,
                        default=Path(os.environ.get("SIMULATOR_STATE_DIR", "/var/lib/solarmind/gateway")))  # fmt: skip
    parser.add_argument(
        "--days", type=int, default=int(os.environ.get("SIMULATOR_HISTORY_DAYS", "14"))
    )
    parser.add_argument(
        "--interval", type=int, default=int(os.environ.get("SIMULATOR_INTERVAL_S", "300"))
    )
    parser.add_argument("--transport", choices=["rest", "mqtt"],
                        default=os.environ.get("SIMULATOR_TRANSPORT", "rest"))  # fmt: skip
    args = parser.parse_args()
    config_path = args.state_dir / "simulator.json"
    if args.command == "seed":
        password = os.environ.get("SIMULATOR_DEMO_PASSWORD")
        if not password or len(password) < 12:
            sys.exit("SIMULATOR_DEMO_PASSWORD must be set (at least 12 characters)")
        asyncio.run(seed(args.api_url, config_path, args.days, args.interval, password))
    else:
        asyncio.run(run(config_path, args.api_url, args.state_dir, args.transport))


if __name__ == "__main__":
    main()
