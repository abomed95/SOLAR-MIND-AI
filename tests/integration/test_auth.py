import psycopg
import pyotp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import make_settings
from tests.helpers import API, PASSWORD, add_member, register

pytestmark = pytest.mark.db


async def test_register_creates_org_admin_and_session(client: AsyncClient) -> None:
    actor = await register(client, "Hotel Kempinski")
    me = (await client.get(f"{API}/auth/me", headers=actor.headers)).json()
    assert me["organization"]["name"] == "Hotel Kempinski"
    assert me["role"] == "ORGANIZATION_ADMIN"
    assert "sites:write" in me["permissions"]
    assert "password" not in str(me)


async def test_register_rejects_duplicate_email_and_weak_password(client: AsyncClient) -> None:
    actor = await register(client)
    payload = {"organization_name": "Other", "full_name": "X", "email": actor.email,
               "password": PASSWORD}  # fmt: skip
    assert (await client.post(f"{API}/auth/register", json=payload)).status_code == 409
    payload = {**payload, "email": "new@example.com", "password": "short"}
    response = await client.post(f"{API}/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_login_failure_is_identical_for_unknown_and_wrong_password(
    client: AsyncClient, owner_dsn: str
) -> None:
    actor = await register(client)
    wrong = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": "Wrong-Password-1!"}
    )
    unknown = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "Wrong-Password-1!"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    with psycopg.connect(owner_dsn) as conn:
        failures = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE action = 'auth.login_failed'"
        ).fetchone()
    assert failures == (2,)


async def test_logout_revokes_the_session(client: AsyncClient) -> None:
    actor = await register(client)
    assert (await client.post(f"{API}/auth/logout", headers=actor.headers)).status_code == 204
    assert (await client.get(f"{API}/auth/me", headers=actor.headers)).status_code == 401


async def test_requests_without_or_with_bad_token_are_rejected(client: AsyncClient) -> None:
    assert (await client.get(f"{API}/sites")).status_code == 401
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert (await client.get(f"{API}/sites", headers=bad)).status_code == 401


async def test_revoking_other_sessions(client: AsyncClient) -> None:
    first = await register(client)
    login = await client.post(
        f"{API}/auth/login", json={"email": first.email, "password": PASSWORD}
    )
    second = {"Authorization": f"Bearer {login.json()['token']}"}
    sessions = (await client.get(f"{API}/auth/sessions", headers=second)).json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1

    response = await client.post(f"{API}/auth/sessions/revoke-others", headers=second)
    assert response.status_code == 204
    assert (await client.get(f"{API}/auth/me", headers=first.headers)).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=second)).status_code == 200


async def test_password_reset_flow(client: AsyncClient, app: FastAPI) -> None:
    actor = await register(client)
    mailer = app.state.mailer

    # Unknown address: same answer, nothing sent.
    response = await client.post(f"{API}/auth/password/forgot", json={"email": "ghost@example.com"})
    assert response.status_code == 202 and mailer.sent == []

    response = await client.post(f"{API}/auth/password/forgot", json={"email": actor.email})
    assert response.status_code == 202
    token = mailer.last_token()

    new_password = "A-Brand-New-Passw0rd"
    body = {"token": token, "new_password": new_password}
    assert (await client.post(f"{API}/auth/password/reset", json=body)).status_code == 204
    # Existing sessions are revoked, the old password no longer works, the token is single-use.
    assert (await client.get(f"{API}/auth/me", headers=actor.headers)).status_code == 401
    old = await client.post(f"{API}/auth/login", json={"email": actor.email, "password": PASSWORD})
    assert old.status_code == 401
    assert (await client.post(f"{API}/auth/password/reset", json=body)).status_code == 400
    new = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": new_password}
    )
    assert new.status_code == 200


async def test_mfa_enrolment_and_login(client: AsyncClient) -> None:
    actor = await register(client)
    setup = (await client.post(f"{API}/auth/mfa/setup", headers=actor.headers)).json()
    totp = pyotp.TOTP(setup["secret"])
    wrong = await client.post(
        f"{API}/auth/mfa/enable", json={"code": "000000"}, headers=actor.headers
    )
    assert wrong.status_code == 400
    ok = await client.post(
        f"{API}/auth/mfa/enable", json={"code": totp.now()}, headers=actor.headers
    )
    assert ok.status_code == 204

    credentials = {"email": actor.email, "password": PASSWORD}
    missing = await client.post(f"{API}/auth/login", json=credentials)
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "mfa_required"
    bad = await client.post(f"{API}/auth/login", json={**credentials, "totp_code": "123456"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "mfa_invalid"
    good = await client.post(f"{API}/auth/login", json={**credentials, "totp_code": totp.now()})
    assert good.status_code == 200


async def test_mfa_secret_is_encrypted_at_rest(client: AsyncClient, owner_dsn: str) -> None:
    actor = await register(client)
    secret = (await client.post(f"{API}/auth/mfa/setup", headers=actor.headers)).json()["secret"]
    with psycopg.connect(owner_dsn) as conn:
        stored = conn.execute("SELECT mfa_secret_encrypted, password_hash FROM users").fetchone()
    assert stored is not None
    assert secret not in stored[0]
    assert stored[1].startswith("$argon2id$")


async def test_cookie_session_requires_csrf_header_on_writes(client: AsyncClient) -> None:
    actor = await register(client)
    client.cookies.set("sm_session", actor.token)
    site = {"name": "S", "installed_capacity_kwp": 10}
    assert (await client.get(f"{API}/sites")).status_code == 200
    blocked = await client.post(f"{API}/sites", json=site)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "csrf"
    allowed = await client.post(f"{API}/sites", json=site, headers={"X-SolarMind-CSRF": "1"})
    assert allowed.status_code == 201


async def test_login_sets_httponly_cookie(client: AsyncClient) -> None:
    actor = await register(client)
    response = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": PASSWORD}
    )
    cookie = response.headers["set-cookie"].lower()
    assert "sm_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie


async def test_auth_endpoints_are_rate_limited(clean: None) -> None:
    from solarmind_api.main import create_app

    app = create_app(make_settings(rate_limit_auth_per_minute=3))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = {"email": "x@example.com", "password": "whatever-password"}
        codes = [(await c.post(f"{API}/auth/login", json=body)).status_code for _ in range(5)]
    await app.state.redis.aclose()
    await app.state.db.dispose()
    assert codes == [401, 401, 401, 429, 429]


async def test_invitation_flow_and_single_use(client: AsyncClient, app: FastAPI) -> None:
    admin = await register(client)
    response = await client.post(
        f"{API}/users/invitations",
        json={"email": "tech@example.com", "role": "TECHNICIAN"},
        headers=admin.headers,
    )
    assert response.status_code == 201
    token = response.json()["invitation_url"].split("token=")[1]
    assert app.state.mailer.sent[-1][0] == "tech@example.com"

    accept = {"token": token, "full_name": "Tech", "password": PASSWORD}
    joined = await client.post(f"{API}/auth/invitations/accept", json=accept)
    assert joined.status_code == 201
    assert joined.json()["role"] == "TECHNICIAN"
    assert joined.json()["organization"]["id"] == admin.org_id
    assert (await client.post(f"{API}/auth/invitations/accept", json=accept)).status_code == 400

    members = (await client.get(f"{API}/users", headers=admin.headers)).json()
    assert {m["role"] for m in members} == {"ORGANIZATION_ADMIN", "TECHNICIAN"}
    # Inviting an existing member again is refused.
    again = await client.post(
        f"{API}/users/invitations",
        json={"email": "tech@example.com", "role": "VIEWER"},
        headers=admin.headers,
    )
    assert again.status_code == 409


async def test_last_admin_cannot_be_demoted_or_removed(client: AsyncClient, app: FastAPI) -> None:
    admin = await register(client)
    demote = await client.patch(
        f"{API}/users/{admin.user_id}", json={"role": "VIEWER"}, headers=admin.headers
    )
    assert demote.status_code == 409 and demote.json()["error"]["code"] == "last_admin"
    remove = await client.delete(f"{API}/users/{admin.user_id}", headers=admin.headers)
    assert remove.status_code == 409

    second = await add_member(client, app, admin, "ORGANIZATION_ADMIN")
    demote = await client.patch(
        f"{API}/users/{admin.user_id}", json={"role": "VIEWER"}, headers=second.headers
    )
    assert demote.status_code == 200


async def test_removed_member_loses_access_immediately(client: AsyncClient, app: FastAPI) -> None:
    admin = await register(client)
    viewer = await add_member(client, app, admin, "VIEWER")
    assert (await client.get(f"{API}/sites", headers=viewer.headers)).status_code == 200
    removed = await client.delete(f"{API}/users/{viewer.user_id}", headers=admin.headers)
    assert removed.status_code == 204
    assert (await client.get(f"{API}/sites", headers=viewer.headers)).status_code == 403


async def test_security_headers_and_error_shape(client: AsyncClient) -> None:
    response = await client.get(f"{API}/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
