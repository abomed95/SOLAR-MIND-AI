import uuid
from datetime import UTC, datetime, timedelta

import pytest

from solarmind_adapters import (
    AdapterError,
    Fault,
    FaultKind,
    MockSolarAdapter,
    PlantConfig,
    PlantModel,
    simulate,
)
from solarmind_schemas import METRICS, Source

# 2026-03-10 00:00 in Djibouti (UTC+3)
DAY = datetime(2026, 3, 9, 21, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def local(hour: float) -> datetime:
    return DAY + timedelta(hours=hour)


def run_day(**config: object) -> list:  # type: ignore[type-arg]
    return simulate(PlantModel(PlantConfig(**config)), DAY, DAY + timedelta(days=1), STEP)  # type: ignore[arg-type]


def test_simulation_is_deterministic_for_a_seed() -> None:
    first = [s.values for s in run_day(seed=7, weather="partly_cloudy")]
    second = [s.values for s in run_day(seed=7, weather="partly_cloudy")]
    other = [s.values for s in run_day(seed=8, weather="partly_cloudy")]
    assert first == second
    assert first != other


def test_production_follows_the_sun() -> None:
    states = run_day(capacity_kwp=100)
    by_hour = {round((s.at - DAY).total_seconds() / 3600, 2): s.values for s in states}
    assert by_hour[2.0]["pv_power_kw"] == 0.0  # night
    assert by_hour[22.0]["pv_power_kw"] == 0.0
    assert 0 < by_hour[7.5]["pv_power_kw"] < by_hour[12.0]["pv_power_kw"]
    assert by_hour[17.0]["pv_power_kw"] < by_hour[12.0]["pv_power_kw"]
    assert 55 < by_hour[12.0]["pv_power_kw"] <= 100  # clear sky, derated by heat and losses
    assert by_hour[12.0]["irradiance_w_m2"] > 800


def test_every_value_is_a_known_metric_within_its_physical_range() -> None:
    for state in run_day(weather="partly_cloudy"):
        for name, value in state.values.items():
            spec = METRICS[name]
            assert spec.minimum <= value <= spec.maximum, (name, value)


def test_power_balance_holds_at_every_step() -> None:
    for state in run_day():
        v = state.values
        supply = v["pv_power_kw"] + v["grid_import_kw"]
        demand = v["load_power_kw"] + v["grid_export_kw"] + v["battery_power_kw"]
        assert supply == pytest.approx(demand, abs=1e-3)


def test_battery_respects_its_limits() -> None:
    states = simulate(PlantModel(PlantConfig(battery_kwh=50, capacity_kwp=20, base_load_kw=30)),
                      DAY, DAY + timedelta(days=2), STEP)  # fmt: skip
    socs = [s.values["battery_soc_percent"] for s in states]
    assert min(socs) >= PlantModel.MIN_SOC - 1e-6  # never discharged below the floor
    assert max(socs) <= 100
    assert all(abs(s.values["battery_power_kw"]) <= 25 + 1e-6 for s in states)  # 0.5 C


def test_daily_counter_integrates_power_and_resets_at_local_midnight() -> None:
    states = simulate(PlantModel(PlantConfig()), DAY, DAY + timedelta(days=2), STEP)
    end_of_day_one = states[287].values["daily_energy_kwh"]  # 23:55 local
    # Reference: trapezoidal integral of the same power samples.
    powers = [s.values["pv_power_kw"] for s in states[:288]]
    expected = sum((a + b) / 2 * (5 / 60) for a, b in zip(powers, powers[1:], strict=False))
    assert end_of_day_one == pytest.approx(expected, rel=1e-3)
    assert 300 < end_of_day_one < 800  # plausible for 100 kWp
    assert states[288].values["daily_energy_kwh"] == 0.0  # 00:00 next day
    assert states[-1].values["total_energy_kwh"] > end_of_day_one  # lifetime keeps growing


def test_weather_reduces_production() -> None:
    def noon(weather: str) -> float:
        return next(s for s in run_day(weather=weather) if s.at == local(12)).values["pv_power_kw"]

    assert noon("overcast") < noon("dusty") < noon("clear")


def test_fault_injection() -> None:
    window = (local(10), local(14))
    healthy = PlantModel(PlantConfig()).state_at(local(12)).values

    hot = PlantModel(PlantConfig(faults=[Fault(FaultKind.OVER_TEMPERATURE, *window, 30)]))
    assert hot.state_at(local(12)).values["inverter_temperature_c"] == pytest.approx(
        healthy["inverter_temperature_c"] + 30, abs=0.01
    )
    assert hot.state_at(local(15)).values["inverter_temperature_c"] < 75  # fault over

    tripped = PlantModel(PlantConfig(faults=[Fault(FaultKind.INVERTER_TRIP, *window)]))
    assert tripped.state_at(local(12)).values["pv_power_kw"] == 0.0

    degrading = PlantModel(PlantConfig(faults=[Fault(FaultKind.PERFORMANCE_LOSS, *window, 0.4)]))
    assert degrading.state_at(local(12)).values["pv_power_kw"] == pytest.approx(
        healthy["pv_power_kw"] * 0.8,
        rel=1e-3,  # half-way through a ramp to -40 %
    )


def test_model_refuses_to_go_back_in_time() -> None:
    plant = PlantModel(PlantConfig())
    plant.state_at(local(12))
    with pytest.raises(ValueError, match="forward"):
        plant.state_at(local(11))


async def test_mock_adapter_reads_its_role_and_labels_data_as_simulated() -> None:
    now = [local(12)]
    plant = PlantModel(PlantConfig())
    inverter = MockSolarAdapter(uuid.uuid4(), plant, "INVERTER", clock=lambda: now[0])
    battery = MockSolarAdapter(uuid.uuid4(), plant, "BATTERY_BMS", clock=lambda: now[0])
    with pytest.raises(AdapterError):
        await inverter.read_measurements()  # must connect first
    await inverter.connect()
    await battery.connect()
    record = await inverter.read_measurements()
    assert record is not None
    assert record.source is Source.SIMULATED
    assert set(record.metrics) == {"pv_power_kw", "daily_energy_kwh", "total_energy_kwh",
                                   "inverter_temperature_c"}  # fmt: skip
    assert record.timestamp == local(12)
    other = await battery.read_measurements()
    assert other is not None and "battery_soc_percent" in other.metrics
    assert (await inverter.get_device_status()).connected


async def test_mock_adapter_communication_loss_and_missing_metrics() -> None:
    now = [local(9)]
    faults = [
        Fault(FaultKind.COMM_LOSS, local(10), local(11)),
        Fault(FaultKind.MISSING_METRICS, local(12), local(13)),
    ]
    adapter = MockSolarAdapter(uuid.uuid4(), PlantModel(PlantConfig(faults=faults)), "INVERTER",
                               clock=lambda: now[0])  # fmt: skip
    await adapter.connect()
    assert await adapter.read_measurements() is not None

    now[0] = local(10.5)
    with pytest.raises(AdapterError, match="communication loss"):
        await adapter.read_measurements()
    status = await adapter.get_device_status()
    assert not status.connected and status.last_error
    with pytest.raises(AdapterError):
        await adapter.connect()  # still unreachable

    now[0] = local(12.5)
    await adapter.connect()  # reconnects once the fault is over
    record = await adapter.read_measurements()
    assert record is not None
    # Dropped metrics are absent, not zero.
    assert set(record.metrics) == {"pv_power_kw", "total_energy_kwh"}
