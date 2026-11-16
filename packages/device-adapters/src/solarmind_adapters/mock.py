"""Simulated solar plant and the mock adapter that reads it.

Everything produced here is SIMULATED and labelled as such. The model is a
plausible physical sketch (clear-sky irradiance, cell temperature derating,
battery dispatch), not a validated digital twin: it exists to exercise the
platform end to end without hardware.

Determinism: for a given configuration and seed, stepping through the same
timestamps always yields the same values.
"""

import math
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from solarmind_adapters.base import AdapterError, DeviceStatus, SolarDeviceAdapter
from solarmind_schemas import Source, TelemetryRecord

Weather = Literal["clear", "partly_cloudy", "overcast", "dusty"]
DeviceRole = Literal["INVERTER", "BATTERY_BMS", "METER", "WEATHER_STATION"]

# Mean and spread of the sky transmission factor per weather type.
_WEATHER: dict[str, tuple[float, float]] = {
    "clear": (0.97, 0.02),
    "partly_cloudy": (0.75, 0.20),
    "overcast": (0.35, 0.10),
    "dusty": (0.80, 0.05),
}

_ROLE_METRICS: dict[str, tuple[str, ...]] = {
    "INVERTER": ("pv_power_kw", "daily_energy_kwh", "total_energy_kwh", "inverter_temperature_c"),
    "BATTERY_BMS": ("battery_soc_percent", "battery_voltage_v", "battery_power_kw",
                    "battery_temperature_c"),
    "METER": ("load_power_kw", "grid_import_kw", "grid_export_kw"),
    "WEATHER_STATION": ("irradiance_w_m2", "ambient_temperature_c"),
}  # fmt: skip


class FaultKind(StrEnum):
    PERFORMANCE_LOSS = "PERFORMANCE_LOSS"  # gradual: output ramps down to (1 - magnitude)
    OVER_TEMPERATURE = "OVER_TEMPERATURE"  # inverter runs `magnitude` degC hotter
    INVERTER_TRIP = "INVERTER_TRIP"  # production drops to zero
    COMM_LOSS = "COMM_LOSS"  # devices do not answer
    MISSING_METRICS = "MISSING_METRICS"  # some metrics are absent from readings
    DUPLICATE = "DUPLICATE"  # the same reading is delivered twice


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    start: datetime
    end: datetime
    magnitude: float = 0.0

    def active(self, at: datetime) -> bool:
        return self.start <= at < self.end

    def progress(self, at: datetime) -> float:
        span = (self.end - self.start).total_seconds()
        return min(1.0, max(0.0, (at - self.start).total_seconds() / span)) if span else 1.0


@dataclass
class PlantConfig:
    capacity_kwp: float = 100.0
    battery_kwh: float = 200.0
    base_load_kw: float = 35.0
    timezone: str = "Africa/Djibouti"
    latitude: float = 11.59
    weather: Weather = "clear"
    seed: int = 1
    initial_soc: float = 60.0
    faults: list[Fault] = field(default_factory=list)


@dataclass(frozen=True)
class PlantState:
    at: datetime
    values: dict[str, float]


class PlantModel:
    """Steps a simulated plant through time. State (battery, counters) is
    path-dependent, so timestamps must be visited in increasing order."""

    SYSTEM_EFFICIENCY = 0.86  # inverter, wiring and soiling losses
    TEMP_COEFFICIENT = -0.004  # per degC above 25 degC cell temperature
    NOCT = 45.0
    BATTERY_EFFICIENCY = 0.95  # one way
    MIN_SOC = 20.0
    MAX_C_RATE = 0.5

    def __init__(self, config: PlantConfig) -> None:
        self.config = config
        self._tz = ZoneInfo(config.timezone)
        self._soc = config.initial_soc
        self._daily_kwh = 0.0
        self._total_kwh = 0.0
        self._state: PlantState | None = None

    def _noise(self, at: datetime, salt: int, bucket_s: int = 600) -> float:
        """Deterministic pseudo-random number in [-1, 1], constant per time bucket."""
        key = (self.config.seed * 1_000_003 + salt) * 1_000_003 + int(at.timestamp()) // bucket_s
        return random.Random(key).uniform(-1.0, 1.0)

    def _fault(self, kind: FaultKind, at: datetime) -> Fault | None:
        return next((f for f in self.config.faults if f.kind == kind and f.active(at)), None)

    def has_fault(self, kind: FaultKind, at: datetime) -> bool:
        return self._fault(kind, at) is not None

    def irradiance(self, at: datetime) -> float:
        """Global irradiance in W/m2 (Haurwitz clear-sky model x sky factor)."""
        local = at.astimezone(self._tz)
        day = local.timetuple().tm_yday
        hour = local.hour + local.minute / 60 + local.second / 3600
        declination = math.radians(23.45) * math.sin(2 * math.pi * (284 + day) / 365)
        latitude = math.radians(self.config.latitude)
        hour_angle = math.radians(15 * (hour - 12))
        cos_zenith = math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(
            declination
        ) * math.cos(hour_angle)
        if cos_zenith <= 0.01:
            return 0.0
        clear_sky = 1098 * cos_zenith * math.exp(-0.057 / cos_zenith)
        mean, spread = _WEATHER[self.config.weather]
        sky = min(1.0, max(0.05, mean + spread * self._noise(at, salt=1)))
        return clear_sky * sky

    def state_at(self, at: datetime) -> PlantState:
        at = at.astimezone(UTC)
        if self._state is not None:
            if at == self._state.at:
                return self._state
            if at < self._state.at:
                raise ValueError("PlantModel must be stepped forward in time")
        cfg = self.config
        previous = self._state
        dt_h = (at - previous.at).total_seconds() / 3600 if previous else 0.0
        local = at.astimezone(self._tz)
        hour = local.hour + local.minute / 60

        irradiance = self.irradiance(at)
        ambient = 31 + 5 * math.sin(2 * math.pi * (hour - 9) / 24) + 0.8 * self._noise(at, 2, 3600)
        cell = ambient + (self.NOCT - 20) / 800 * irradiance

        pv = cfg.capacity_kwp * irradiance / 1000 * self.SYSTEM_EFFICIENCY
        pv *= 1 + self.TEMP_COEFFICIENT * (cell - 25)
        loss = self._fault(FaultKind.PERFORMANCE_LOSS, at)
        if loss:
            pv *= 1 - loss.magnitude * loss.progress(at)
        if self._fault(FaultKind.INVERTER_TRIP, at):
            pv = 0.0
        pv = min(max(pv, 0.0), cfg.capacity_kwp)

        # Daytime-heavy commercial profile with a small evening shoulder.
        profile = 0.55 + 0.45 * math.exp(-(((hour - 13) / 4.5) ** 2))
        load = max(0.0, cfg.base_load_kw * profile * (1 + 0.06 * self._noise(at, 3)))

        # Battery dispatch: charge on surplus, discharge on deficit, grid takes the rest.
        net = pv - load
        battery_kw = 0.0
        if cfg.battery_kwh > 0 and dt_h > 0:
            max_kw = cfg.battery_kwh * self.MAX_C_RATE
            if net > 0 and self._soc < 100:
                room_kwh = (100 - self._soc) / 100 * cfg.battery_kwh
                battery_kw = min(net, max_kw, room_kwh / (dt_h * self.BATTERY_EFFICIENCY))
            elif net < 0 and self._soc > self.MIN_SOC:
                available_kwh = (self._soc - self.MIN_SOC) / 100 * cfg.battery_kwh
                battery_kw = -min(-net, max_kw, available_kwh * self.BATTERY_EFFICIENCY / dt_h)
            stored = battery_kw * dt_h
            stored *= self.BATTERY_EFFICIENCY if stored > 0 else 1 / self.BATTERY_EFFICIENCY
            self._soc = min(100.0, max(0.0, self._soc + stored / cfg.battery_kwh * 100))
        grid = net - battery_kw

        if previous and local.date() != previous.at.astimezone(self._tz).date():
            self._daily_kwh = 0.0
        if previous:
            energy = (pv + previous.values["pv_power_kw"]) / 2 * dt_h
            self._daily_kwh += energy
            self._total_kwh += energy

        inverter_temp = ambient + 28 * (pv / cfg.capacity_kwp) + 0.5 * self._noise(at, 4)
        hot = self._fault(FaultKind.OVER_TEMPERATURE, at)
        if hot:
            inverter_temp += hot.magnitude

        values = {
            "pv_power_kw": pv,
            "daily_energy_kwh": self._daily_kwh,
            "total_energy_kwh": self._total_kwh,
            "inverter_temperature_c": inverter_temp,
            "battery_soc_percent": self._soc,
            "battery_voltage_v": 46 + 8 * self._soc / 100,
            "battery_power_kw": battery_kw,
            "battery_temperature_c": ambient + 2 + 0.02 * abs(battery_kw),
            "load_power_kw": load,
            "grid_import_kw": max(0.0, -grid),
            "grid_export_kw": max(0.0, grid),
            "irradiance_w_m2": irradiance,
            "ambient_temperature_c": ambient,
        }
        self._state = PlantState(at, {k: round(v, 4) for k, v in values.items()})
        return self._state


class MockSolarAdapter(SolarDeviceAdapter):
    """Reads one device role of a simulated plant. Fully functional, no hardware."""

    def __init__(
        self,
        device_id: uuid.UUID,
        plant: PlantModel,
        role: DeviceRole,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(device_id)
        self._plant, self._role, self._clock = plant, role, clock
        self._connected = False
        self._last_success: datetime | None = None
        self._last_error: str | None = None

    async def connect(self) -> None:
        if self._plant.has_fault(FaultKind.COMM_LOSS, self._clock()):
            self._last_error = "simulated communication loss"
            raise AdapterError(self._last_error)
        self._connected = True

    async def read_measurements(self) -> TelemetryRecord | None:
        now = self._clock()
        if not self._connected:
            raise AdapterError("not connected")
        if self._plant.has_fault(FaultKind.COMM_LOSS, now):
            self._connected = False
            self._last_error = "simulated communication loss"
            raise AdapterError(self._last_error)
        state = self._plant.state_at(now)
        metrics: dict[str, float | None] = {m: state.values[m] for m in _ROLE_METRICS[self._role]}
        if self._plant.has_fault(FaultKind.MISSING_METRICS, now):
            # Drop every other metric: absent, not zero.
            metrics = {m: v for i, (m, v) in enumerate(metrics.items()) if i % 2 == 0}
        self._last_success, self._last_error = now, None
        return TelemetryRecord(
            device_id=self.device_id, timestamp=state.at, metrics=metrics, source=Source.SIMULATED
        )

    async def get_device_status(self) -> DeviceStatus:
        return DeviceStatus(self._connected, self._last_success, self._last_error)

    async def disconnect(self) -> None:
        self._connected = False


def simulate(
    plant: PlantModel, start: datetime, end: datetime, interval: timedelta
) -> list[PlantState]:
    """Step ``plant`` from ``start`` (inclusive) to ``end`` (exclusive)."""
    states, at = [], start
    while at < end:
        states.append(plant.state_at(at))
        at += interval
    return states
