"""Background worker: MQTT ingestion and periodic jobs.

Run with ``python -m solarmind_api.worker``. Every job is wrapped so that one
failure is logged and retried on the next cycle instead of killing the worker.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

import aiomqtt

from solarmind_api.config import Settings, get_settings
from solarmind_api.db import Database
from solarmind_api.logging import configure_logging
from solarmind_api.services import alerts, ingestion, notifications
from solarmind_api.services.mail import build_mailer

logger = logging.getLogger("solarmind_api.worker")
MQTT_TOPIC = "solarmind/v1/+/telemetry"


async def mqtt_consumer(db: Database, settings: Settings, stop: asyncio.Event) -> None:
    if settings.mqtt_host is None:
        logger.warning("MQTT ingestion disabled: SOLARMIND_MQTT_HOST is not set")
        return
    backoff = 1.0
    while not stop.is_set():
        try:
            async with aiomqtt.Client(
                settings.mqtt_host,
                settings.mqtt_port,
                username=settings.mqtt_username,
                password=settings.mqtt_password.get_secret_value()
                if settings.mqtt_password
                else None,
                # Durable session: the broker queues QoS 1 messages while the
                # worker is down, so nothing published in the meantime is lost.
                identifier="solarmind-worker",
                clean_session=False,
            ) as client:
                await client.subscribe(MQTT_TOPIC, qos=1)
                logger.info("mqtt connected", extra={"host": settings.mqtt_host})
                backoff = 1.0
                async for message in client.messages:
                    payload = message.payload if isinstance(message.payload, bytes) else b""
                    try:
                        await ingestion.handle_mqtt_message(
                            db, settings, str(message.topic), payload
                        )
                    except Exception:
                        logger.exception("mqtt message failed", extra={"topic": str(message.topic)})
                    if stop.is_set():
                        break
        except aiomqtt.MqttError as exc:
            logger.warning("mqtt disconnected, reconnecting",
                           extra={"error": str(exc), "retry_in_s": backoff})  # fmt: skip
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                backoff = min(backoff * 2, 60.0)


async def periodic(
    name: str, interval_s: float, job: Callable[[], Awaitable[object]], stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            outcome = await job()
            logger.debug("job done", extra={"job": name, "outcome": str(outcome)})
        except Exception:
            logger.exception("job failed", extra={"job": name})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue


async def run(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    # Background jobs scan all tenants: no API statement timeout here.
    db = Database.from_settings(settings, statement_timeout=False)
    providers = notifications.build_providers(settings, build_mailer(settings))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def offline_job() -> tuple[int, int]:
        async with db.system() as s:
            return await alerts.detect_offline_devices(s)

    async def notification_job() -> dict[str, int]:
        return await notifications.dispatch_pending(db, settings, providers)

    logger.info("worker started")
    await asyncio.gather(
        mqtt_consumer(db, settings, stop),
        periodic("offline-detection", 60, offline_job, stop),
        periodic("notification-dispatch", 30, notification_job, stop),
    )
    await db.dispose()
    logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
