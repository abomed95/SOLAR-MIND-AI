"""Demo plant definitions and (de)serialization of simulator configuration.

Everything defined here is fictitious demonstration data. Names are generic on
purpose: no real customer or installation is represented.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from solarmind_adapters import Fault, FaultKind, PlantConfig


@dataclass(frozen=True)
class FaultSpec:
    """A fault anchored to a local calendar day before the seeding instant, so
    that its effect does not depend on the time of day the seed is run."""

    kind: FaultKind
    days_ago: int
    start_hour: float  # local time
    duration_hours: float
    magnitude: float = 0.0

    def resolve(self, reference: datetime, timezone: str) -> Fault:
        local = reference.astimezone(ZoneInfo(timezone))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days=self.days_ago) + timedelta(hours=self.start_hour)
        return Fault(self.kind, start, start + timedelta(hours=self.duration_hours), self.magnitude)


@dataclass(frozen=True)
class SiteSpec:
    name: str
    kind: str
    capacity_kwp: float
    battery_kwh: float
    base_load_kw: float
    weather: str = "clear"
    latitude: float = 11.59
    longitude: float = 43.15
    tariff_per_kwh: str = "55"
    faults: tuple[FaultSpec, ...] = ()


@dataclass(frozen=True)
class OrgSpec:
    name: str
    admin_email: str
    sites: tuple[SiteSpec, ...] = field(default_factory=tuple)


DEMO_ORGANIZATIONS: tuple[OrgSpec, ...] = (
    OrgSpec(
        "Demo Hotel Group (simulated)",
        "admin@hotel.demo.solarmind.example",
        (
            SiteSpec("Seaside Hotel", "HOTEL", 250, 400, 110,
                     faults=(FaultSpec(FaultKind.OVER_TEMPERATURE, 2, 11, 3, 40),)),
            SiteSpec("City Hotel", "HOTEL", 120, 150, 60, weather="partly_cloudy",
                     faults=(FaultSpec(FaultKind.PERFORMANCE_LOSS, 10, 0, 480, 0.3),)),
        ),
    ),
    OrgSpec(
        "Demo Industrial Park (simulated)",
        "admin@industry.demo.solarmind.example",
        (
            SiteSpec("Cold Storage Plant", "INDUSTRIAL", 800, 1000, 380, weather="dusty",
                     faults=(FaultSpec(FaultKind.COMM_LOSS, 1, 10, 2),)),
            SiteSpec("Logistics Warehouse", "COMMERCIAL", 300, 0, 90),
        ),
    ),
    OrgSpec(
        "Demo Public Health Agency (simulated)",
        "admin@health.demo.solarmind.example",
        (
            SiteSpec("District Hospital", "HOSPITAL", 400, 900, 170,
                     faults=(FaultSpec(FaultKind.INVERTER_TRIP, 3, 12, 1.5),
                             FaultSpec(FaultKind.MISSING_METRICS, 1, 8, 4))),
        ),
    ),
)  # fmt: skip


def roles_for(site: SiteSpec) -> list[str]:
    roles = ["INVERTER", "METER", "WEATHER_STATION"]
    if site.battery_kwh > 0:
        roles.insert(1, "BATTERY_BMS")
    return roles


def plant_config_to_json(config: PlantConfig) -> dict[str, Any]:
    return {
        "capacity_kwp": config.capacity_kwp,
        "battery_kwh": config.battery_kwh,
        "base_load_kw": config.base_load_kw,
        "timezone": config.timezone,
        "latitude": config.latitude,
        "weather": config.weather,
        "seed": config.seed,
        "initial_soc": config.initial_soc,
        "faults": [
            {
                "kind": f.kind,
                "start": f.start.isoformat(),
                "end": f.end.isoformat(),
                "magnitude": f.magnitude,
            }  # fmt: skip
            for f in config.faults
        ],
    }


def plant_config_from_json(data: dict[str, Any]) -> PlantConfig:
    faults = [
        Fault(
            FaultKind(f["kind"]),
            datetime.fromisoformat(f["start"]),
            datetime.fromisoformat(f["end"]),
            f["magnitude"],
        )  # fmt: skip
        for f in data.get("faults", [])
    ]
    return PlantConfig(**{**data, "faults": faults})
