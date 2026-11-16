"""Outbound e-mail behind an interface.

Without SMTP settings the ``LogMailer`` is used: it records that a message was
*not* sent. It never pretends delivery happened.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from solarmind_api.config import Settings

logger = logging.getLogger(__name__)


class Mailer(Protocol):
    name: str

    async def send(self, to: str, subject: str, body: str) -> None: ...


class LogMailer:
    name = "log"

    def __init__(self, *, include_body: bool) -> None:
        self._include_body = include_body

    async def send(self, to: str, subject: str, body: str) -> None:
        extra = {"to": to, "subject": subject}
        if self._include_body:  # development only: bodies can contain one-time links
            extra["body"] = body
        logger.warning("e-mail NOT sent: SMTP is not configured", extra=extra)


class SmtpMailer:
    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self._s.smtp_from, to, subject
        message.set_content(body)
        if self._s.smtp_host is None:
            raise RuntimeError("SMTP host is not configured")
        with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if self._s.smtp_username and self._s.smtp_password:
                smtp.login(self._s.smtp_username, self._s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def send(self, to: str, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send_sync, to, subject, body)


def build_mailer(settings: Settings) -> Mailer:
    if settings.smtp_host:
        return SmtpMailer(settings)
    return LogMailer(include_body=settings.environment in ("development", "test"))
