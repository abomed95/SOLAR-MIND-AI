"""Edge gateway: local buffering and replay (no server needed)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from solarmind_adapters import (
    Fault,
    FaultKind,
    GatewayRunner,
    MockSolarAdapter,
    Outbox,
    PlantConfig,
    PlantModel,
    TransportError,
)
from solarmind_schemas import TelemetryRecord

T0 = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


def record(minute: int) -> TelemetryRecord:
    return TelemetryRecord(
        device_id=uuid.UUID(int=1), timestamp=T0 + timedelta(minutes=minute),
        metrics={"pv_power_kw": float(minute)},
    )  # fmt: skip


class FlakyTransport:
    def __init__(self) -> None:
        self.online = True
        self.batches: list[list[TelemetryRecord]] = []
        self.reported_backlog: list[int] = []

    async def send(self, records: list[TelemetryRecord], buffered_records: int) -> None:
        if not self.online:
            raise TransportError("network down")
        self.batches.append(records)
        self.reported_backlog.append(buffered_records)


def test_outbox_is_durable_fifo(tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    outbox = Outbox(path)
    for minute in range(5):
        outbox.put(record(minute))
    outbox.close()

    reopened = Outbox(path)  # survives a gateway restart
    assert reopened.count() == 5
    batch = reopened.peek(3)
    assert [r.metrics["pv_power_kw"] for _, r in batch] == [0.0, 1.0, 2.0]
    assert reopened.count() == 5  # peek does not remove
    reopened.ack([row_id for row_id, _ in batch])
    assert [r.metrics["pv_power_kw"] for _, r in reopened.peek(10)] == [3.0, 4.0]


def test_outbox_cap_drops_oldest_and_counts_the_loss(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "o.sqlite3", max_rows=3)
    for minute in range(5):
        outbox.put(record(minute))
    assert outbox.count() == 3 and outbox.dropped == 2
    assert [r.metrics["pv_power_kw"] for _, r in outbox.peek(10)] == [2.0, 3.0, 4.0]


async def test_records_are_kept_while_offline_and_replayed_in_order(tmp_path: Path) -> None:
    now = [T0]
    plant = PlantModel(PlantConfig())
    adapters = [
        MockSolarAdapter(uuid.uuid4(), plant, "INVERTER", clock=lambda: now[0]),
        MockSolarAdapter(uuid.uuid4(), plant, "METER", clock=lambda: now[0]),
    ]
    outbox, transport = Outbox(tmp_path / "o.sqlite3"), FlakyTransport()
    runner = GatewayRunner(adapters, outbox, transport, batch_size=5)

    transport.online = False
    for _ in range(6):  # 30 minutes offline
        await runner.poll_once()
        assert await runner.flush() == 0
        now[0] += timedelta(minutes=5)
    assert outbox.count() == 12 and transport.batches == []

    transport.online = True
    assert await runner.flush() == 12
    assert outbox.count() == 0
    sent = [r for batch in transport.batches for r in batch]
    assert [len(b) for b in transport.batches] == [5, 5, 2]
    assert transport.reported_backlog == [7, 2, 0]
    assert [r.timestamp for r in sent] == sorted(r.timestamp for r in sent)
    assert await runner.flush() == 0  # nothing is sent twice


async def test_a_failed_batch_is_not_acknowledged(tmp_path: Path) -> None:
    class FailsOnSecondBatch(FlakyTransport):
        async def send(self, records: list[TelemetryRecord], buffered_records: int) -> None:
            if len(self.batches) == 1:
                raise TransportError("connection reset")
            await super().send(records, buffered_records)

    outbox = Outbox(tmp_path / "o.sqlite3")
    for minute in range(6):
        outbox.put(record(minute))
    runner = GatewayRunner([], outbox, FailsOnSecondBatch(), batch_size=3)
    assert await runner.flush() == 3
    assert [r.metrics["pv_power_kw"] for _, r in outbox.peek(10)] == [3.0, 4.0, 5.0]


async def test_runner_reconnects_after_device_communication_loss(tmp_path: Path) -> None:
    now = [T0]
    plant = PlantModel(PlantConfig(
        faults=[Fault(FaultKind.COMM_LOSS, T0 + timedelta(minutes=5), T0 + timedelta(minutes=15))]
    ))  # fmt: skip
    adapter = MockSolarAdapter(uuid.uuid4(), plant, "INVERTER", clock=lambda: now[0])
    outbox = Outbox(tmp_path / "o.sqlite3")
    runner = GatewayRunner([adapter], outbox, FlakyTransport())

    buffered = []
    for _ in range(5):  # 0, 5, 10 (lost), 15, 20
        buffered.append(await runner.poll_once())
        now[0] += timedelta(minutes=5)
    # Nothing is invented for the two polls made during the outage.
    assert buffered == [1, 0, 0, 1, 1]
    assert outbox.count() == 3
