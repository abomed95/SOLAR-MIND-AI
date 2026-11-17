"""Outbound notification delivery with retries.

Deliveries are rows of an outbox table written in the same transaction as the
alert. The worker sends them; a failure is retried with exponential backoff
and marked FAILED after ``notification_max_attempts``.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select

from solarmind_api.config import Settings
from solarmind_api.db import Database
from solarmind_api.enums import Channel, DeliveryStatus
from solarmind_api.models import NotificationDelivery
from solarmind_api.services.mail import Mailer

logger = logging.getLogger(__name__)


class ProviderNotConfigured(Exception):
    """The channel has no provider: retrying cannot help."""


class ChannelProvider(Protocol):
    async def send(self, recipient: str, subject: str, body: str) -> None: ...


class EmailChannel:
    def __init__(self, mailer: Mailer) -> None:
        self._mailer = mailer

    async def send(self, recipient: str, subject: str, body: str) -> None:
        if self._mailer.name == "log":
            raise ProviderNotConfigured("SMTP is not configured")
        await self._mailer.send(recipient, subject, body)


class WhatsAppNotConfigured:
    """Placeholder adapter. No WhatsApp Business provider has been selected or
    credentialed, so nothing is sent and the delivery is marked FAILED with an
    explicit reason. Implement ``ChannelProvider`` against the chosen provider
    (e.g. Meta Cloud API or an aggregator) and register it in ``build_providers``."""

    async def send(self, recipient: str, subject: str, body: str) -> None:
        raise ProviderNotConfigured("WhatsApp provider is not configured")


def build_providers(settings: Settings, mailer: Mailer) -> dict[str, ChannelProvider]:
    return {Channel.EMAIL: EmailChannel(mailer), Channel.WHATSAPP: WhatsAppNotConfigured()}


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * 2 ** (attempts - 1)))


# A claimed delivery is invisible to other workers for this long. If the
# worker dies mid-send, the delivery becomes due again after the lease
# (at-least-once delivery: a crash may cause one duplicate, never a loss).
CLAIM_LEASE = timedelta(minutes=5)


async def dispatch_pending(
    db: Database, settings: Settings, providers: dict[str, ChannelProvider], batch: int = 100
) -> dict[str, int]:
    """Send due deliveries in three short steps: claim, send, record.

    No database transaction (nor row lock, nor pooled connection) is held
    while talking to SMTP or WhatsApp: a slow provider can no longer pin a
    connection for the whole batch.
    """
    counts = {"sent": 0, "retry": 0, "failed": 0}
    now = datetime.now(UTC)
    async with db.system() as s:
        claimed = (
            await s.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == DeliveryStatus.PENDING,
                    NotificationDelivery.next_attempt_at <= now,
                )
                .order_by(NotificationDelivery.next_attempt_at)
                .limit(batch)
                .with_for_update(skip_locked=True)  # safe with several workers
            )
        ).all()
        jobs = [(d.id, d.channel, d.recipient, d.subject, d.body, d.attempts) for d in claimed]
        for delivery in claimed:
            delivery.next_attempt_at = now + CLAIM_LEASE
    if not jobs:
        return counts

    outcomes: dict[uuid.UUID, tuple[str, str | None]] = {}
    for delivery_id, channel, recipient, subject, body, _ in jobs:
        try:
            await providers[channel].send(recipient, subject, body)
        except ProviderNotConfigured as exc:
            outcomes[delivery_id] = ("unconfigured", str(exc))
        except Exception as exc:  # noqa: BLE001 - any provider error is retryable
            outcomes[delivery_id] = ("error", repr(exc)[:500])
        else:
            outcomes[delivery_id] = ("sent", None)

    async with db.system() as s:
        rows = await s.scalars(
            select(NotificationDelivery).where(NotificationDelivery.id.in_(list(outcomes)))
        )
        for delivery in rows:
            outcome, error = outcomes[delivery.id]
            delivery.attempts += 1
            delivery.last_error = error
            if outcome == "sent":
                delivery.status = DeliveryStatus.SENT
                counts["sent"] += 1
            elif (
                outcome == "unconfigured" or delivery.attempts >= settings.notification_max_attempts
            ):
                delivery.status = DeliveryStatus.FAILED
                counts["failed"] += 1
            else:
                delivery.next_attempt_at = datetime.now(UTC) + backoff(delivery.attempts)
                counts["retry"] += 1
    if any(counts.values()):
        logger.info("notifications dispatched", extra=counts)
    return counts
