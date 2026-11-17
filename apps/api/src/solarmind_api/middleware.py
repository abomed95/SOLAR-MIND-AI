"""HTTP edge: request identity, access log, security headers, body size limit.

Written as plain ASGI middleware rather than ``@app.middleware("http")``
(Starlette's ``BaseHTTPMiddleware``), which wraps every request in an extra
task and memory stream: measurable overhead and known problems with
streaming responses.
"""

import logging
import re
import time
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Any

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("solarmind_api.access")

# Read by the JSON log formatter: every log line of a request carries its id.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
INGEST_PREFIX = "/api/v1/ingestion/"

Network = IPv4Network | IPv6Network


class PayloadTooLarge(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(413, f"Request body too large (max {limit} bytes)")


def resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: Iterable[Network]
) -> str:
    """Client address, honouring ``X-Forwarded-For`` only from trusted proxies.

    The header is read right to left, skipping trusted hops: the first
    untrusted address is the client. Entries left of it were written by the
    client itself and are never believed.
    """
    trusted = list(trusted)

    def is_trusted(value: str) -> bool:
        try:
            address = ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in trusted)

    if not peer:
        return "unknown"
    if not forwarded_for or not is_trusted(peer):
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        try:
            ip_address(hop)
        except ValueError:
            return peer  # malformed chain: fall back to what TCP says
        if not is_trusted(hop):
            return hop
    return peer


class EdgeMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        security_headers: dict[str, str],
        trusted_proxies: Iterable[Network],
        max_body_bytes: int,
        max_ingest_body_bytes: int,
    ) -> None:
        self.app = app
        self.headers = [(k.lower().encode(), v.encode()) for k, v in security_headers.items()]
        self.trusted = list(trusted_proxies)
        self.max_body = max_body_bytes
        self.max_ingest_body = max_ingest_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        peer = scope["client"][0] if scope.get("client") else None
        client = resolve_client_ip(peer, headers.get("x-forwarded-for"), self.trusted)
        scope.setdefault("state", {})["client_ip"] = client
        path: str = scope["path"]
        limit = self.max_ingest_body if path.startswith(INGEST_PREFIX) else self.max_body
        status = 500

        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            # Refused before a single byte of the body is read.
            await self._reject(send, request_id, limit)
            self._log(scope, client, request_id, 413, started)
            request_id_var.reset(token)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # chunked body without Content-Length
                    raise PayloadTooLarge(limit)
            return message

        docs = path.startswith("/api/docs")

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                raw: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw}
                if not docs:
                    raw += [(k, v) for k, v in self.headers if k not in present]
                elapsed = (time.perf_counter() - started) * 1000
                raw.append((b"x-request-id", request_id.encode()))
                raw.append((b"server-timing", f"app;dur={elapsed:.1f}".encode()))
                message = {**message, "headers": raw}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        finally:
            self._log(scope, client, request_id, status, started)
            request_id_var.reset(token)

    async def _reject(self, send: Send, request_id: str, limit: int) -> None:
        body = (
            '{"error":{"code":"payload_too_large","message":"Request body too large '
            f'(max {limit} bytes)","details":null}}}}'
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"x-request-id", request_id.encode()),
                *self.headers,
            ],
        })  # fmt: skip
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _log(scope: Scope, client: str, request_id: str, status: int, started: float) -> None:
        # Path only: query strings may carry identifiers we do not want in logs.
        extra: dict[str, Any] = {
            "method": scope["method"],
            "path": scope["path"],
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "client_ip": client,
            "request_id": request_id,
        }
        # Probes hit /health every few seconds: keep them out of the INFO stream.
        level = logging.DEBUG if scope["path"].startswith("/api/v1/health") else logging.INFO
        logger.log(level, "request", extra=extra)
