import asyncio
import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from solarmind_api.logging import JsonFormatter
from solarmind_api.main import create_app
from tests.conftest import make_settings


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("secret-host.internal refused")


async def _hang() -> None:
    await asyncio.sleep(30)


async def test_live_reports_version(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_ready_when_all_dependencies_answer(app: FastAPI, client: AsyncClient) -> None:
    app.state.probes = {"database": _ok, "redis": _ok}
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_not_ready_when_one_dependency_fails(app: FastAPI, client: AsyncClient) -> None:
    app.state.probes = {"database": _fail, "redis": _ok}
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "unavailable", "redis": "ok"}
    # The failure reason must stay in the logs, never in the response body.
    assert "secret-host" not in response.text


async def test_not_ready_when_a_probe_hangs(client: AsyncClient) -> None:
    app = create_app(make_settings(readiness_timeout_s=0.05))
    app.state.probes = {"database": _hang}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/api/v1/health/ready")
    assert response.status_code == 503


async def test_default_probes_fail_closed_without_services(client: AsyncClient) -> None:
    """Real probes against unreachable services must report 503, not crash."""
    if "SOLARMIND_DATABASE_URL" in __import__("os").environ:
        pytest.skip("real services configured; covered by the integration test")
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "unavailable", "redis": "unavailable"}


async def test_openapi_disabled_in_production() -> None:
    app = create_app(make_settings(environment="production"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/openapi.json")).status_code == 404
        assert (await c.get("/api/docs")).status_code == 404
        assert (await c.get("/api/v1/health/live")).status_code == 200


def test_settings_require_connection_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from solarmind_api.config import Settings

    monkeypatch.delenv("SOLARMIND_DATABASE_URL", raising=False)
    monkeypatch.delenv("SOLARMIND_REDIS_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_do_not_leak_secrets_in_repr() -> None:
    settings = make_settings(database_url="postgresql+asyncpg://u:hunter2@h/db")
    assert "hunter2" not in repr(settings)


def test_json_formatter_emits_one_parsable_object() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.probe = "database"
    line = JsonFormatter().format(record)
    parsed = json.loads(line)
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["probe"] == "database"
    assert parsed["ts"].endswith("+00:00")


@pytest.mark.integration
async def test_ready_against_real_services(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}
