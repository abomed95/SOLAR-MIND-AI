"""Read-only device adapters and the edge gateway runtime."""

from solarmind_adapters.base import AdapterError, DeviceStatus, SolarDeviceAdapter
from solarmind_adapters.gateway import (
    GatewayRunner,
    MqttTransport,
    Outbox,
    RestTransport,
    Transport,
    TransportError,
)
from solarmind_adapters.mock import (
    Fault,
    FaultKind,
    MockSolarAdapter,
    PlantConfig,
    PlantModel,
    simulate,
)

__all__ = [
    "AdapterError", "DeviceStatus", "Fault", "FaultKind", "GatewayRunner", "MockSolarAdapter",
    "MqttTransport", "Outbox", "PlantConfig", "PlantModel", "RestTransport",
    "SolarDeviceAdapter", "Transport", "TransportError", "simulate",
]  # fmt: skip
