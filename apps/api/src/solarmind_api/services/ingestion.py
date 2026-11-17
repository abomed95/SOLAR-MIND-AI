"""Telemetry ingestion: validation, idempotent storage, device/gateway liveness.

Incoming payloads are untrusted. Nothing in a payload decides which
organization the data belongs to: that comes from the authenticated gateway.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import DateTime, column, func, select, update, values
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.config import Settings
from solarmind_api.db import Database
from solarmind_api.errors import AppError, Unauthorized
from solarmind_api.models import Device, Gateway, Telemetry
from solarmind_api.security import hash_token
from solarmind_schemas import METRICS, Quality, Source, TelemetryBatch

logger = logging.getLogger(__name__)

# asyncpg allows 32767 bind parameters per statement; 9 columns per row.
_ROWS_PER_STATEMENT = 3000
MAX_AGE = timedelta(days=730)  # matches the raw retention policy


@dataclass
class IngestResult:
    received: int = 0  # metric values received
    inserted: int = 0
    duplicates: int = 0
    out_of_range: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    # Rows actually stored, for downstream evaluation (alerts).
    rows: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "out_of_range": self.out_of_range,
            "rejected": self.rejected,
        }


async def authenticate_gateway(db: Database, api_key: str | None) -> Gateway:
    if not api_key:
        raise Unauthorized("Gateway key required", code="invalid_gateway_key")
    async with db.system() as s:
        gateway = await s.scalar(select(Gateway).where(Gateway.api_key_hash == hash_token(api_key)))
    if gateway is None:
        raise Unauthorized("Invalid gateway key", code="invalid_gateway_key")
    return gateway


async def ingest(
    s: AsyncSession, settings: Settings, gateway: Gateway, batch: TelemetryBatch
) -> IngestResult:
    """Store a batch for ``gateway``. ``s`` must be a tenant transaction of the
    gateway's organization."""
    if len(batch.records) > settings.max_batch_records:
        raise AppError(
            f"Batch too large (max {settings.max_batch_records} records)",
            code="batch_too_large",
            status_code=413,
        )
    now = datetime.now(UTC)
    result = IngestResult()
    # A gateway may only report devices of its own site.
    devices = {
        d.id: d
        for d in await s.scalars(
            select(Device).where(
                Device.organization_id == gateway.organization_id,
                Device.site_id == gateway.site_id,
            )
        )
    }

    rows: list[dict[str, Any]] = []
    seen: set[tuple[uuid.UUID, str, datetime]] = set()
    latest: dict[uuid.UUID, datetime] = {}
    for index, record in enumerate(batch.records):
        result.received += len(record.metrics)
        device = devices.get(record.device_id)
        if device is None:
            result.rejected.append({"index": index, "reason": "unknown_device"})
            continue
        if record.timestamp > now + timedelta(seconds=settings.max_future_skew_s):
            result.rejected.append({"index": index, "reason": "timestamp_in_future"})
            continue
        if record.timestamp < now - MAX_AGE:
            result.rejected.append({"index": index, "reason": "timestamp_too_old"})
            continue
        # A simulated device can never produce MEASURED data, whatever it claims.
        source = Source.SIMULATED if device.is_simulated else record.source
        for name, value in record.metrics.items():
            spec = METRICS.get(name)
            if spec is None:
                result.rejected.append({"index": index, "reason": "unknown_metric", "metric": name})
                continue
            if value is None:
                continue  # not measured: absent, never zero
            key = (record.device_id, name, record.timestamp)
            if key in seen:
                result.duplicates += 1
                continue
            seen.add(key)
            quality = record.quality
            if not spec.minimum <= value <= spec.maximum:
                quality = Quality.OUT_OF_RANGE  # kept as received, flagged
                result.out_of_range += 1
            rows.append(
                {
                    "device_id": record.device_id,
                    "metric_name": name,
                    "time": record.timestamp,
                    "organization_id": gateway.organization_id,
                    "site_id": gateway.site_id,
                    "value": value,
                    "quality": quality.value,
                    "source": source.value,
                }
            )
        latest[record.device_id] = max(
            latest.get(record.device_id, record.timestamp), record.timestamp
        )

    stored: set[tuple[uuid.UUID, str, datetime]] = set()
    for start in range(0, len(rows), _ROWS_PER_STATEMENT):
        chunk = rows[start : start + _ROWS_PER_STATEMENT]
        returned = await s.execute(
            pg_insert(Telemetry)
            .values(chunk)
            .on_conflict_do_nothing()  # primary key (device, metric, time): replay-safe
            .returning(Telemetry.device_id, Telemetry.metric_name, Telemetry.time)
        )
        stored.update((d, m, t) for d, m, t in returned)
    result.inserted = len(stored)
    result.duplicates += len(rows) - len(stored)
    result.rows = [r for r in rows if (r["device_id"], r["metric_name"], r["time"]) in stored]

    if latest:
        # One UPDATE ... FROM (VALUES ...) for the whole batch instead of one
        # statement per device. GREATEST keeps late (buffered) data from
        # moving last_seen_at backwards.
        seen_at_by_device = values(
            column("id", PG_UUID(as_uuid=True)),
            column("seen_at", DateTime(timezone=True)),
            name="seen",
        ).data(list(latest.items()))
        await s.execute(
            update(Device)
            .where(Device.id == seen_at_by_device.c.id)
            .values(
                last_seen_at=func.greatest(
                    func.coalesce(Device.last_seen_at, seen_at_by_device.c.seen_at),
                    seen_at_by_device.c.seen_at,
                )
            )
        )
    await s.execute(
        update(Gateway)
        .where(Gateway.id == gateway.id)
        .values(last_seen_at=now, buffered_records=batch.buffered_records)
    )
    return result


async def ingest_for_gateway(
    db: Database, settings: Settings, gateway: Gateway, batch: TelemetryBatch
) -> IngestResult:
    from solarmind_api.services import alerts

    async with db.tenant(gateway.organization_id) as s:
        result = await ingest(s, settings, gateway, batch)
        await alerts.evaluate_rows(s, gateway.organization_id, result.rows)
    return result


async def handle_mqtt_message(
    db: Database, settings: Settings, topic: str, payload: bytes
) -> IngestResult | None:
    """Process one MQTT message. Invalid messages are logged and dropped: a
    poisoned message must never block the consumer."""
    try:
        body = json.loads(payload)
        api_key = body.pop("api_key", None) if isinstance(body, dict) else None
        batch = TelemetryBatch.model_validate(body)
        gateway = await authenticate_gateway(db, api_key)
        # Topic: solarmind/v1/<gateway_id>/telemetry. The key must match it.
        if topic.split("/")[2:3] != [str(gateway.id)]:
            raise Unauthorized("Gateway key does not match the topic")
        result = await ingest_for_gateway(db, settings, gateway, batch)
    except (ValueError, ValidationError, AppError) as exc:
        logger.warning("mqtt message rejected", extra={"topic": topic, "error": str(exc)[:300]})
        return None
    logger.info("mqtt batch ingested", extra={"topic": topic, **result.summary()})
    return result
