"""Common, read-only device adapter interface.

Adapters only ever *read*. There is deliberately no write/command method: the
platform must not be able to actuate electrical equipment.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from solarmind_schemas import TelemetryRecord


class AdapterError(Exception):
    """The device could not be reached or returned unusable data."""


@dataclass(frozen=True)
class DeviceStatus:
    connected: bool
    last_success_at: datetime | None
    last_error: str | None = None


class SolarDeviceAdapter(ABC):
    def __init__(self, device_id: uuid.UUID) -> None:
        self.device_id = device_id

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection. Raises AdapterError on failure."""

    @abstractmethod
    async def read_measurements(self) -> TelemetryRecord | None:
        """Read one set of measurements in canonical units.

        Returns ``None`` when the device has nothing to report. Raises
        AdapterError on communication failure. Never fabricates a value.
        """

    @abstractmethod
    async def get_device_status(self) -> DeviceStatus: ...

    @abstractmethod
    async def disconnect(self) -> None: ...
