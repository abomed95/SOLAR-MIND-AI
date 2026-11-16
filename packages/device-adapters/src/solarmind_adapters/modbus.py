"""Generic Modbus TCP / RTU adapters (read-only).

No manufacturer register map is shipped: inventing one would produce numbers
that look real and are wrong. The integrator supplies the map from the
equipment's official documentation as a list of ``RegisterSpec``.

Only function codes 03 (read holding registers) and 04 (read input registers)
are ever issued. There is no write path.

Validation status: the TCP adapter is tested against a software Modbus server
(pymodbus). Neither adapter has been validated against physical equipment;
the RTU adapter additionally has never been run on a real serial line.
"""

import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from solarmind_adapters.base import AdapterError, DeviceStatus, SolarDeviceAdapter
from solarmind_schemas import METRICS, Source, TelemetryRecord

DataType = Literal["uint16", "int16", "uint32", "int32", "float32"]
_FORMATS: dict[str, tuple[int, str]] = {
    "uint16": (1, "H"), "int16": (1, "h"), "uint32": (2, "I"), "int32": (2, "i"),
    "float32": (2, "f"),
}  # fmt: skip


@dataclass(frozen=True)
class RegisterSpec:
    metric: str  # canonical metric name (see solarmind_schemas.METRICS)
    address: int  # zero-based protocol address
    data_type: DataType = "uint16"
    scale: float = 1.0  # canonical_value = raw * scale + offset
    offset: float = 0.0
    table: Literal["holding", "input"] = "holding"
    word_order: Literal["big", "little"] = "big"  # order of the two 16-bit words

    def __post_init__(self) -> None:
        if self.metric not in METRICS:
            raise ValueError(f"unknown canonical metric '{self.metric}'")
        if not 0 <= self.address <= 0xFFFF:
            raise ValueError("register address out of range")


def decode(spec: RegisterSpec, registers: list[int]) -> float:
    count, fmt = _FORMATS[spec.data_type]
    words = registers[:count]
    if count == 2 and spec.word_order == "little":
        words = words[::-1]
    raw = struct.unpack(f">{fmt}", struct.pack(f">{count}H", *words))[0]
    return float(raw) * spec.scale + spec.offset


class _ModbusAdapter(SolarDeviceAdapter):
    def __init__(self, device_id: uuid.UUID, client: Any, unit: int,
                 registers: list[RegisterSpec]) -> None:  # fmt: skip
        super().__init__(device_id)
        if not registers:
            raise ValueError("a register map is required")
        self._client, self._unit, self._registers = client, unit, registers
        self._last_success: datetime | None = None
        self._last_error: str | None = None

    async def connect(self) -> None:
        try:
            connected = await self._client.connect()
        except (ModbusException, OSError) as exc:
            self._last_error = repr(exc)
            raise AdapterError(f"connection failed: {exc!r}") from exc
        if not connected:
            self._last_error = "connection refused"
            raise AdapterError(self._last_error)

    async def read_measurements(self) -> TelemetryRecord | None:
        metrics: dict[str, float | None] = {}
        for spec in self._registers:
            count = _FORMATS[spec.data_type][0]
            read = (
                self._client.read_holding_registers
                if spec.table == "holding"
                else self._client.read_input_registers
            )
            try:
                response = await read(spec.address, count=count, slave=self._unit)
            except (ModbusException, OSError) as exc:
                self._last_error = repr(exc)
                raise AdapterError(f"read failed: {exc!r}") from exc
            if response.isError():
                # One bad register must not invent a value: the metric is absent.
                self._last_error = f"exception response for '{spec.metric}'"
                continue
            metrics[spec.metric] = decode(spec, list(response.registers))
        if not metrics:
            raise AdapterError(self._last_error or "no register could be read")
        self._last_success = datetime.now(UTC)
        return TelemetryRecord(
            device_id=self.device_id, timestamp=self._last_success, metrics=metrics,
            source=Source.MEASURED,
        )  # fmt: skip

    async def get_device_status(self) -> DeviceStatus:
        return DeviceStatus(bool(self._client.connected), self._last_success, self._last_error)

    async def disconnect(self) -> None:
        self._client.close()


class ModbusTCPAdapter(_ModbusAdapter):
    def __init__(self, device_id: uuid.UUID, *, host: str, port: int = 502, unit: int = 1,
                 registers: list[RegisterSpec], timeout_s: float = 3.0) -> None:  # fmt: skip
        client = AsyncModbusTcpClient(host, port=port, timeout=timeout_s, retries=1)
        super().__init__(device_id, client, unit, registers)


class ModbusRTUAdapter(_ModbusAdapter):
    def __init__(self, device_id: uuid.UUID, *, port: str, baudrate: int = 9600,
                 parity: Literal["N", "E", "O"] = "N", stopbits: int = 1, unit: int = 1,
                 registers: list[RegisterSpec], timeout_s: float = 3.0) -> None:  # fmt: skip
        client = AsyncModbusSerialClient(
            port, baudrate=baudrate, parity=parity, stopbits=stopbits, timeout=timeout_s, retries=1
        )
        super().__init__(device_id, client, unit, registers)
