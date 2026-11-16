"""Modbus adapter against a *software* Modbus TCP server (pymodbus).

This validates framing, decoding and error handling over a real TCP socket.
It is not a validation against physical equipment.
"""

import asyncio
import socket
import struct
import uuid
from collections.abc import AsyncIterator

import pytest
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import ModbusTcpServer

from solarmind_adapters.base import AdapterError
from solarmind_adapters.modbus import ModbusTCPAdapter, RegisterSpec, decode
from solarmind_schemas import Source


def _words(fmt: str, value: float) -> list[int]:
    return list(struct.unpack(">2H", struct.pack(f">{fmt}", value)))


@pytest.mark.parametrize(
    ("spec", "registers", "expected"),
    [
        (RegisterSpec("pv_power_kw", 0, "uint16", scale=0.1), [1255], 125.5),
        (RegisterSpec("battery_power_kw", 0, "int16", scale=0.01), [0xFF9C], -1.0),
        (RegisterSpec("total_energy_kwh", 0, "uint32"), [0x0001, 0x86A0], 100000.0),
        (RegisterSpec("total_energy_kwh", 0, "uint32", word_order="little"), [0x86A0, 0x0001],
         100000.0),
        (RegisterSpec("battery_power_kw", 0, "int32", scale=0.001), [0xFFFF, 0xFC18], -1.0),
        (RegisterSpec("ac_voltage_v", 0, "float32"), _words("f", 230.5), 230.5),
        (RegisterSpec("inverter_temperature_c", 0, "int16", scale=0.1, offset=-10), [425], 32.5),
    ],
)  # fmt: skip
def test_decode(spec: RegisterSpec, registers: list[int], expected: float) -> None:
    assert decode(spec, registers) == pytest.approx(expected, rel=1e-6)


def test_register_spec_rejects_unknown_metric_and_bad_address() -> None:
    with pytest.raises(ValueError, match="unknown canonical metric"):
        RegisterSpec("made_up_metric", 0)
    with pytest.raises(ValueError, match="address"):
        RegisterSpec("pv_power_kw", 70000)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# A register map invented for THIS TEST SERVER ONLY (not any real device).
TEST_MAP = [
    RegisterSpec("pv_power_kw", 0, "uint16", scale=0.1),
    RegisterSpec("inverter_temperature_c", 1, "int16", scale=0.1),
    RegisterSpec("total_energy_kwh", 2, "uint32"),
    RegisterSpec("ac_voltage_v", 10, "float32", table="input"),
]


@pytest.fixture
async def modbus_server() -> AsyncIterator[int]:
    port = _free_port()
    # pymodbus datastores are 1-based: block address 1 is protocol address 0.
    holding = ModbusSequentialDataBlock(1, [1255, 425, 0x0001, 0x86A0] + [0] * 20)
    inputs = ModbusSequentialDataBlock(1, [0] * 10 + _words("f", 230.5) + [0] * 10)
    context = ModbusServerContext(slaves=ModbusSlaveContext(hr=holding, ir=inputs), single=True)
    server = ModbusTcpServer(context, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    yield port
    await server.shutdown()
    task.cancel()


async def test_tcp_adapter_reads_and_normalizes(modbus_server: int) -> None:
    adapter = ModbusTCPAdapter(uuid.uuid4(), host="127.0.0.1", port=modbus_server,
                               registers=TEST_MAP)  # fmt: skip
    await adapter.connect()
    record = await adapter.read_measurements()
    await adapter.disconnect()
    assert record is not None
    assert record.source is Source.MEASURED
    assert record.metrics == pytest.approx(
        {
            "pv_power_kw": 125.5,
            "inverter_temperature_c": 42.5,
            "total_energy_kwh": 100000.0,
            "ac_voltage_v": 230.5,
        }  # fmt: skip
    )
    status = await adapter.get_device_status()
    assert status.last_success_at is not None


async def test_unreadable_register_is_absent_not_zero(modbus_server: int) -> None:
    registers = [TEST_MAP[0], RegisterSpec("battery_soc_percent", 5000)]  # outside the server map
    adapter = ModbusTCPAdapter(uuid.uuid4(), host="127.0.0.1", port=modbus_server,
                               registers=registers)  # fmt: skip
    await adapter.connect()
    record = await adapter.read_measurements()
    await adapter.disconnect()
    assert record is not None
    assert record.metrics == {"pv_power_kw": 125.5}


async def test_connection_failure_raises_adapter_error() -> None:
    adapter = ModbusTCPAdapter(uuid.uuid4(), host="127.0.0.1", port=_free_port(),
                               registers=TEST_MAP, timeout_s=0.5)  # fmt: skip
    with pytest.raises(AdapterError):
        await adapter.connect()


def test_adapters_expose_no_write_method() -> None:
    """Hardware integrations are read-only by construction."""
    forbidden = ("write", "set_", "command", "control")
    public = [name for name in dir(ModbusTCPAdapter) if not name.startswith("_")]
    assert not [name for name in public if any(word in name.lower() for word in forbidden)]
