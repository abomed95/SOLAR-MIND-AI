"""Liveness and readiness endpoints.

``/live`` only says the process answers. ``/ready`` actually probes each
dependency and returns 503 if any of them fails, so an orchestrator never
routes traffic to an instance that cannot reach its database.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from solarmind_api import __version__

Probe = Callable[[], Awaitable[None]]
CheckStatus = Literal["ok", "unavailable"]

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus]


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(version=__version__)


async def _run_probe(name: str, probe: Probe, timeout_s: float) -> CheckStatus:
    try:
        await asyncio.wait_for(probe(), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        # The reason goes to the logs only: connection errors can carry hostnames.
        logger.warning("readiness probe failed", extra={"probe": name, "error": repr(exc)})
        return "unavailable"
    return "ok"


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(request: Request, response: Response) -> ReadyResponse:
    probes: dict[str, Probe] = request.app.state.probes
    timeout_s: float = request.app.state.settings.readiness_timeout_s
    results = await asyncio.gather(*(_run_probe(n, p, timeout_s) for n, p in probes.items()))
    checks = dict(zip(probes.keys(), results, strict=True))
    if all(result == "ok" for result in results):
        return ReadyResponse(status="ready", checks=checks)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="not_ready", checks=checks)
