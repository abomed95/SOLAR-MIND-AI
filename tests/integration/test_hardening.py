"""Performance and security hardening of the 2026-11 audit, against real services."""

from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pyotp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import APP_URL, CapturingMailer, make_settings
from tests.helpers import API, PASSWORD, register

pytestmark = pytest.mark.db


async def _client(**overrides: Any) -> tuple[FastAPI, AsyncClient]:
    from solarmind_api.main import create_app

    app = create_app(make_settings(**overrides))
    app.state.mailer = CapturingMailer()
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def tuned(clean: None, request: pytest.FixtureRequest) -> AsyncIterator[AsyncClient]:
    """Client of an app built with the test's ``settings`` marker overrides."""
    marker = request.node.get_closest_marker("settings")
    app, client = await _client(**(marker.kwargs if marker else {}))
    async with client:
        yield client
    await app.state.redis.aclose()
    await app.state.db.dispose()


# --------------------------------------------------------------------------- edge


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    generated = await client.get(f"{API}/health/live")
    assert len(generated.headers["x-request-id"]) == 32
    assert generated.headers["server-timing"].startswith("app;dur=")
    echoed = await client.get(f"{API}/health/live", headers={"X-Request-ID": "gw-42-abcdef"})
    assert echoed.headers["x-request-id"] == "gw-42-abcdef"
    # Anything that could break a log line or a header is replaced.
    forged = await client.get(f"{API}/health/live", headers={"X-Request-ID": "a b\tc"})
    assert forged.headers["x-request-id"] != "a b\tc"


async def test_security_headers_and_no_hsts_over_plain_http(client: AsyncClient) -> None:
    response = await client.get(f"{API}/health/live")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in response.headers


@pytest.mark.settings(cookie_secure=True)
async def test_hsts_when_deployed_over_https(tuned: AsyncClient) -> None:
    response = await tuned.get(f"{API}/health/live")
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")


@pytest.mark.settings(max_request_body_bytes=1024)
async def test_oversized_bodies_are_refused_before_parsing(tuned: AsyncClient) -> None:
    big = {"email": "x@example.com", "password": "p" * 2000}
    declared = await tuned.post(f"{API}/auth/login", json=big)
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "payload_too_large"

    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length header
        yield b'{"email": "x@example.com", "password": "'
        yield b"p" * 4000
        yield b'"}'

    streamed = await tuned.post(
        f"{API}/auth/login", content=chunks(), headers={"content-type": "application/json"}
    )
    assert streamed.status_code == 413


@pytest.mark.settings(max_request_body_bytes=1024, max_ingest_body_bytes=1024 * 1024)
async def test_ingestion_has_its_own_larger_body_limit(tuned: AsyncClient) -> None:
    payload = {"records": [{"device_id": "00000000-0000-0000-0000-000000000000",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "metrics": {"pv_power_kw": 1.0}}] * 40}  # fmt: skip
    response = await tuned.post(f"{API}/ingestion/batch", json=payload)
    assert response.status_code == 401  # past the size check, stopped by authentication


@pytest.mark.settings(trusted_proxies=["127.0.0.0/8"], rate_limit_auth_per_minute=2)
async def test_rate_limit_follows_the_client_behind_a_trusted_proxy(tuned: AsyncClient) -> None:
    body = {"email": "x@example.com", "password": "whatever-password"}

    async def attempt(ip: str) -> int:
        response = await tuned.post(f"{API}/auth/login", json=body,
                                    headers={"X-Forwarded-For": ip})  # fmt: skip
        return response.status_code

    assert [await attempt("198.51.100.1") for _ in range(3)] == [401, 401, 429]
    # Another user behind the same proxy keeps their own budget.
    assert await attempt("198.51.100.2") == 401


# --------------------------------------------------------------------------- accounts


@pytest.mark.settings(login_max_failures=3)
async def test_account_locks_after_repeated_failures_from_any_address(tuned: AsyncClient) -> None:
    actor = await register(tuned)
    wrong = {"email": actor.email, "password": "not-the-password!"}
    codes = [(await tuned.post(f"{API}/auth/login", json=wrong)).status_code for _ in range(3)]
    assert codes == [401, 401, 401]
    # Even the right password is refused during the lock: guessing is pointless.
    locked = await tuned.post(f"{API}/auth/login", json={"email": actor.email,
                                                         "password": PASSWORD})  # fmt: skip
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "account_locked"


async def test_successful_login_resets_the_failure_counter(
    client: AsyncClient, app: FastAPI
) -> None:
    from solarmind_api.services.throttle import digest

    actor = await register(client)
    for _ in range(3):
        await client.post(
            f"{API}/auth/login", json={"email": actor.email, "password": "nope-nope!"}
        )
    key = f"lock:login:{digest(actor.email)}"
    assert int(await app.state.redis.get(key)) == 3
    ok = await client.post(f"{API}/auth/login", json={"email": actor.email, "password": PASSWORD})
    assert ok.status_code == 200
    assert await app.state.redis.get(key) is None


async def _enable_mfa(client: AsyncClient) -> tuple[Any, pyotp.TOTP]:
    actor = await register(client)
    setup = (await client.post(f"{API}/auth/mfa/setup", headers=actor.headers)).json()
    totp = pyotp.TOTP(setup["secret"])
    enabled = await client.post(
        f"{API}/auth/mfa/enable", json={"code": totp.now()}, headers=actor.headers
    )
    assert enabled.status_code == 204
    return actor, totp


async def test_a_totp_code_cannot_be_replayed(client: AsyncClient) -> None:
    actor, totp = await _enable_mfa(client)
    credentials = {"email": actor.email, "password": PASSWORD, "totp_code": totp.now()}
    assert (await client.post(f"{API}/auth/login", json=credentials)).status_code == 200
    replay = await client.post(f"{API}/auth/login", json=credentials)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "mfa_invalid"


@pytest.mark.settings(mfa_max_failures=2)
async def test_mfa_guessing_is_capped_per_account(tuned: AsyncClient) -> None:
    actor, totp = await _enable_mfa(tuned)
    credentials = {"email": actor.email, "password": PASSWORD}
    for _ in range(2):
        bad = await tuned.post(f"{API}/auth/login", json={**credentials, "totp_code": "000000"})
        assert bad.status_code == 401
    locked = await tuned.post(f"{API}/auth/login", json={**credentials, "totp_code": totp.now()})
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "account_locked"


@pytest.mark.settings(rate_limit_ai_per_minute=2)
async def test_assistant_is_rate_limited_per_user(tuned: AsyncClient) -> None:
    alice, bob = await register(tuned, "Org A"), await register(tuned, "Org B")

    async def ask(actor: Any) -> int:
        response = await tuned.post(f"{API}/ai/ask", json={"question": "Quelle production ?"},
                                    headers=actor.headers)  # fmt: skip
        return response.status_code

    assert [await ask(alice) for _ in range(3)] == [200, 200, 429]
    assert await ask(bob) == 200  # budgets are per user, not global


# --------------------------------------------------------------------------- cache


@pytest.mark.settings(cache_ttl_overview_s=60)
async def test_overview_cache_is_per_organization(tuned: AsyncClient) -> None:
    alice, bob = await register(tuned, "Org A"), await register(tuned, "Org B")
    await tuned.post(
        f"{API}/sites",
        headers=alice.headers,
        json={"name": "Site Alice", "installed_capacity_kwp": 50.0},
    )

    first = await tuned.get(f"{API}/energy/overview", headers=alice.headers)
    second = await tuned.get(f"{API}/energy/overview", headers=alice.headers)
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert first.json() == second.json() and second.json()["site_count"] == 1

    other = await tuned.get(f"{API}/energy/overview", headers=bob.headers)
    assert other.headers["x-cache"] == "MISS" and other.json()["site_count"] == 0


@pytest.mark.settings(cache_ttl_overview_s=60)
async def test_site_changes_invalidate_the_cache(tuned: AsyncClient) -> None:
    alice = await register(tuned)
    assert (await tuned.get(f"{API}/energy/overview", headers=alice.headers)).json()[
        "site_count"
    ] == 0
    created = await tuned.post(f"{API}/sites", headers=alice.headers,
                               json={"name": "New site", "installed_capacity_kwp": 10.0})  # fmt: skip
    assert created.status_code == 201
    after = await tuned.get(f"{API}/energy/overview", headers=alice.headers)
    assert after.headers["x-cache"] == "MISS" and after.json()["site_count"] == 1


@pytest.mark.settings(cache_ttl_overview_s=60)
async def test_cache_is_consulted_only_after_authorization(tuned: AsyncClient) -> None:
    alice = await register(tuned)
    await tuned.get(f"{API}/energy/overview", headers=alice.headers)  # warm the cache
    tuned.cookies.clear()
    assert (await tuned.get(f"{API}/energy/overview")).status_code == 401


# --------------------------------------------------------------------------- database


async def test_rls_predicates_are_evaluated_once_per_statement(owner_dsn: str) -> None:
    with psycopg.connect(owner_dsn) as conn:
        quals = conn.execute(
            "SELECT tablename, qual FROM pg_policies WHERE schemaname = 'public'"
        ).fetchall()
    assert quals, "no policy found"
    for table, qual in quals:
        assert "( SELECT app_rls_bypass()" in qual, (table, qual)


async def test_statement_timeout_cancels_runaway_queries(_database: str) -> None:
    from solarmind_api.db import Database

    db = Database(APP_URL, statement_timeout_ms=100)
    try:
        with pytest.raises(DBAPIError, match="statement timeout"):
            async with db.system() as s:
                await s.execute(text("SELECT pg_sleep(1)"))
    finally:
        await db.dispose()
