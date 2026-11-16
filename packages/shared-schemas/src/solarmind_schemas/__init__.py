"""Canonical telemetry contract shared by gateways, the API and the agents.

Units are fixed per metric (SI-derived engineering units). A value outside the
physical range is kept but flagged; a missing value is absent, never zero.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


class Quality(StrEnum):
    VALID = "valid"
    OUT_OF_RANGE = "out_of_range"
    SUSPECT = "suspect"


class Source(StrEnum):
    MEASURED = "MEASURED"
    SIMULATED = "SIMULATED"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    unit: str
    minimum: float
    maximum: float
    # How values of several devices of one site combine into a site figure.
    site_aggregation: Literal["sum", "avg"]
    description: str


_SPECS = [
    MetricSpec("pv_power_kw", "kW", 0, 1e6, "sum", "AC power produced by the PV system"),
    MetricSpec("daily_energy_kwh", "kWh", 0, 1e7, "sum", "Inverter energy counter since midnight"),
    MetricSpec("total_energy_kwh", "kWh", 0, 1e12, "sum", "Inverter lifetime energy counter"),
    MetricSpec("load_power_kw", "kW", 0, 1e6, "sum", "Power consumed by the site"),
    MetricSpec("grid_import_kw", "kW", 0, 1e6, "sum", "Power drawn from the grid"),
    MetricSpec("grid_export_kw", "kW", 0, 1e6, "sum", "Power injected into the grid"),
    MetricSpec("battery_soc_percent", "%", 0, 100, "avg", "Battery state of charge"),
    MetricSpec("battery_voltage_v", "V", 0, 1500, "avg", "Battery DC voltage"),
    MetricSpec("battery_power_kw", "kW", -1e6, 1e6, "sum", "Battery power, positive = charging"),
    MetricSpec("battery_temperature_c", "degC", -40, 120, "avg", "Battery temperature"),
    MetricSpec("inverter_temperature_c", "degC", -40, 150, "avg", "Inverter internal temperature"),
    MetricSpec("irradiance_w_m2", "W/m2", 0, 1500, "avg", "Plane-of-array irradiance"),
    MetricSpec("ambient_temperature_c", "degC", -50, 70, "avg", "Ambient air temperature"),
    MetricSpec("ac_voltage_v", "V", 0, 1000, "avg", "AC voltage"),
    MetricSpec("grid_frequency_hz", "Hz", 0, 70, "avg", "Grid frequency"),
]
METRICS: dict[str, MetricSpec] = {spec.name: spec for spec in _SPECS}


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _finite(values: dict[str, float | None]) -> dict[str, float | None]:
    for name, value in values.items():
        if value is not None and not math.isfinite(value):
            raise ValueError(f"metric '{name}' is not a finite number")
    return values


class TelemetryRecord(BaseModel):
    """One reading of one device at one instant."""

    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    timestamp: Annotated[AwareDatetime, AfterValidator(_to_utc)]
    # ``null`` means "not measured": it is skipped, never stored as zero.
    metrics: Annotated[
        dict[str, float | None], Field(min_length=1, max_length=64), AfterValidator(_finite)
    ]
    quality: Quality = Quality.VALID
    source: Source = Source.MEASURED


class TelemetryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: Annotated[list[TelemetryRecord], Field(min_length=1)]
    # Records still waiting in the gateway's local buffer after this batch.
    buffered_records: Annotated[int, Field(ge=0)] | None = None


__all__ = ["METRICS", "MetricSpec", "Quality", "Source", "TelemetryBatch", "TelemetryRecord"]
