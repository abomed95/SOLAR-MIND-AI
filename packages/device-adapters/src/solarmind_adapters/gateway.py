"""Edge gateway runtime: poll adapters, buffer locally, forward when online.

Delivery is at-least-once: a record leaves the SQLite outbox only after the
platform acknowledged the batch. Replays are harmless because ingestion is
idempotent on (device, metric, timestamp).
"""

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import httpx

from solarmind_adapters.base import AdapterError, SolarDeviceAdapter
from solarmind_schemas import TelemetryRecord

logger = logging.getLogger(__name__)


class TransportError(Exception):
    """The batch was not acknowledged; it must stay in the outbox."""


class Outbox:
    """Durable FIFO of records waiting to be delivered (SQLite, WAL mode)."""

    def __init__(self, path: str | Path, max_rows: int = 500_000) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "payload TEXT NOT NULL)"
        )
        self._db.commit()
        self._max_rows = max_rows
        self.dropped = 0

    def put(self, record: TelemetryRecord) -> None:
        self._db.execute("INSERT INTO outbox (payload) VALUES (?)", (record.model_dump_json(),))
        overflow = self.count() - self._max_rows
        if overflow > 0:
            # Disk is finite: beyond the cap the OLDEST records are discarded,
            # and the loss is counted and logged, never silent.
            self._db.execute(
                "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)",
                (overflow,),
            )
            self.dropped += overflow
            logger.error("outbox full, oldest records dropped", extra={"dropped": overflow})
        self._db.commit()

    def peek(self, limit: int) -> list[tuple[int, TelemetryRecord]]:
        rows = self._db.execute(
            "SELECT id, payload FROM outbox ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [(row_id, TelemetryRecord.model_validate_json(payload)) for row_id, payload in rows]

    def ack(self, ids: Sequence[int]) -> None:
        self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
        self._db.commit()

    def count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def close(self) -> None:
        self._db.close()


class Transport(Protocol):
    async def send(self, records: list[TelemetryRecord], buffered_records: int) -> None:
        """Deliver a batch or raise TransportError."""


def _payload(records: list[TelemetryRecord], buffered_records: int) -> dict[str, object]:
    return {
        "records": [r.model_dump(mode="json") for r in records],
        "buffered_records": buffered_records,
    }


class RestTransport:
    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client, self._api_key = client, api_key

    async def send(self, records: list[TelemetryRecord], buffered_records: int) -> None:
        try:
            response = await self._client.post(
                "/api/v1/ingestion/batch",
                json=_payload(records, buffered_records),
                headers={"X-Gateway-Key": self._api_key},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"network error: {exc!r}") from exc
        # 4xx other than 429 will not succeed on retry, but dropping data on a
        # configuration error (e.g. rotated key) would be worse: keep it.
        if response.status_code != 200:
            raise TransportError(f"HTTP {response.status_code}")


class MqttTransport:
    """Publishes with QoS 1. The broker persists messages for the platform's
    durable subscription, so a platform restart does not lose them."""

    def __init__(self, *, host: str, port: int, username: str, password: str,
                 gateway_id: uuid.UUID, api_key: str) -> None:  # fmt: skip
        self._host, self._port, self._username, self._password = host, port, username, password
        self._topic = f"solarmind/v1/{gateway_id}/telemetry"
        self._api_key = api_key

    async def send(self, records: list[TelemetryRecord], buffered_records: int) -> None:
        import aiomqtt

        body = {**_payload(records, buffered_records), "api_key": self._api_key}
        try:
            async with aiomqtt.Client(
                self._host, self._port, username=self._username, password=self._password,
                timeout=10,
            ) as client:  # fmt: skip
                await client.publish(self._topic, json.dumps(body), qos=1)
        except aiomqtt.MqttError as exc:
            raise TransportError(f"mqtt error: {exc!r}") from exc


class GatewayRunner:
    def __init__(self, adapters: Sequence[SolarDeviceAdapter], outbox: Outbox,
                 transport: Transport, batch_size: int = 500) -> None:  # fmt: skip
        self._adapters, self._outbox, self._transport = list(adapters), outbox, transport
        self._batch_size = batch_size
        self._connected: set[uuid.UUID] = set()
        self.duplicate_next = False  # test/simulation hook: store the next readings twice

    async def poll_once(self) -> int:
        """Read every adapter once into the outbox. Returns records buffered."""
        buffered = 0
        for adapter in self._adapters:
            try:
                if adapter.device_id not in self._connected:
                    await adapter.connect()  # automatic reconnection, every cycle
                    self._connected.add(adapter.device_id)
                record = await adapter.read_measurements()
            except AdapterError as exc:
                self._connected.discard(adapter.device_id)
                logger.warning("device read failed",
                               extra={"device_id": str(adapter.device_id), "error": str(exc)})  # fmt: skip
                continue
            if record is None:
                continue
            for _ in range(2 if self.duplicate_next else 1):
                self._outbox.put(record)
                buffered += 1
        return buffered

    async def flush(self) -> int:
        """Send buffered records oldest first. Stops at the first failure and
        keeps everything not acknowledged. Returns records delivered."""
        delivered = 0
        while batch := self._outbox.peek(self._batch_size):
            remaining = self._outbox.count() - len(batch)
            try:
                await self._transport.send([r for _, r in batch], remaining)
            except TransportError as exc:
                logger.warning("delivery failed, records kept in the local buffer",
                               extra={"buffered": self._outbox.count(), "error": str(exc)})  # fmt: skip
                break
            self._outbox.ack([row_id for row_id, _ in batch])
            delivered += len(batch)
        return delivered

    async def run(self, interval_s: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll_once()
            await self.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                continue
        for adapter in self._adapters:
            await adapter.disconnect()
