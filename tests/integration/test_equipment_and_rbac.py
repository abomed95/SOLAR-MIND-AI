import psycopg
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.helpers import API, add_member, create_device, create_gateway, create_site, register

pytestmark = pytest.mark.db


async def test_site_crud_and_audit(client: AsyncClient) -> None:
    admin = await register(client)
    site = await create_site(client, admin, "Plant 1", kind="SOLAR_PLANT")
    assert site["timezone"] == "Africa/Djibouti" and site["currency"] == "DJF"

    url = f"{API}/sites/{site['id']}"
    updated = await client.patch(url, json={"name": "Plant One"}, headers=admin.headers)
    assert updated.json()["name"] == "Plant One"
    assert updated.json()["installed_capacity_kwp"] == 100.0
    assert (await client.delete(url, headers=admin.headers)).status_code == 204
    assert (await client.get(url, headers=admin.headers)).status_code == 404

    logs = (
        await client.get(f"{API}/organizations/current/audit-logs", headers=admin.headers)
    ).json()
    actions = [entry["action"] for entry in logs["items"]]
    assert actions[:3] == ["site.delete", "site.update", "site.create"]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "installed_capacity_kwp": 10},
        {"name": "x", "installed_capacity_kwp": -5},
        {"name": "x", "installed_capacity_kwp": 10, "timezone": "Mars/Olympus"},
        {"name": "x", "installed_capacity_kwp": 10, "latitude": 123},
        {
            "name": "x",
            "installed_capacity_kwp": 10,
            "organization_id": "00000000-0000-0000-0000-000000000000",
        },
        {"name": "x", "installed_capacity_kwp": 10, "is_simulated": "maybe"},
        {"name": "x"},
    ],
)
async def test_site_validation(client: AsyncClient, payload: dict) -> None:  # type: ignore[type-arg]
    admin = await register(client)
    response = await client.post(f"{API}/sites", json=payload, headers=admin.headers)
    assert response.status_code == 422, response.text


async def test_listing_pagination_filter_and_sort(client: AsyncClient) -> None:
    admin = await register(client)
    for name, capacity in (("Bravo", 30), ("Alpha", 20), ("Charlie", 10)):
        await create_site(client, admin, name, installed_capacity_kwp=capacity)

    page = (await client.get(f"{API}/sites?limit=2", headers=admin.headers)).json()
    assert [s["name"] for s in page["items"]] == ["Alpha", "Bravo"]
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)
    page = (await client.get(f"{API}/sites?limit=2&offset=2", headers=admin.headers)).json()
    assert [s["name"] for s in page["items"]] == ["Charlie"]

    by_capacity = await client.get(
        f"{API}/sites?sort=-installed_capacity_kwp", headers=admin.headers
    )
    assert [s["name"] for s in by_capacity.json()["items"]] == ["Bravo", "Alpha", "Charlie"]
    filtered = (await client.get(f"{API}/sites?q=alp", headers=admin.headers)).json()
    assert [s["name"] for s in filtered["items"]] == ["Alpha"]

    bad_sort = await client.get(f"{API}/sites?sort=password_hash", headers=admin.headers)
    assert bad_sort.status_code == 422 and bad_sort.json()["error"]["code"] == "invalid_sort"
    assert (await client.get(f"{API}/sites?limit=9999", headers=admin.headers)).status_code == 422


async def test_device_and_gateway_registration(client: AsyncClient) -> None:
    admin = await register(client)
    site = await create_site(client, admin)
    gateway = await create_gateway(client, admin, site["id"])
    assert len(gateway["api_key"]) >= 40 and gateway["status"] == "NEVER_SEEN"
    device = await create_device(client, admin, site["id"], gateway_id=gateway["id"])
    assert device["status"] == "NEVER_SEEN" and device["gateway_id"] == gateway["id"]

    listed = (await client.get(f"{API}/devices?site_id={site['id']}", headers=admin.headers)).json()
    assert listed["total"] == 1
    # The key is shown once: listing never returns it, and only a hash is stored.
    gateways = (await client.get(f"{API}/gateways", headers=admin.headers)).json()
    assert "api_key" not in gateways[0] and "api_key_hash" not in gateways[0]

    rotated = await client.post(f"{API}/gateways/{gateway['id']}/rotate-key", headers=admin.headers)
    assert rotated.json()["api_key"] != gateway["api_key"]


async def test_plan_limits_are_enforced(client: AsyncClient, owner_dsn: str) -> None:
    admin = await register(client)
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute("UPDATE subscriptions SET plan = 'ESSENTIAL'")
    site = await create_site(client, admin, "One")
    await create_site(client, admin, "Two")
    third = await client.post(
        f"{API}/sites", json={"name": "Three", "installed_capacity_kwp": 5}, headers=admin.headers
    )
    assert third.status_code == 402
    assert third.json()["error"]["code"] == "plan_limit_exceeded"
    assert third.json()["error"]["details"] == {"resource": "sites", "limit": 2, "current": 2}

    for i in range(10):
        await create_device(client, admin, site["id"], f"D{i}")
    eleventh = await client.post(
        f"{API}/devices", json={"site_id": site["id"], "name": "D10", "kind": "METER"},
        headers=admin.headers,
    )  # fmt: skip
    assert eleventh.status_code == 402

    # 5 users: the admin plus 4 invitations (pending invitations hold a seat).
    for i in range(4):
        ok = await client.post(
            f"{API}/users/invitations", json={"email": f"u{i}@example.com", "role": "VIEWER"},
            headers=admin.headers,
        )  # fmt: skip
        assert ok.status_code == 201
    over = await client.post(
        f"{API}/users/invitations", json={"email": "u5@example.com", "role": "VIEWER"},
        headers=admin.headers,
    )  # fmt: skip
    assert over.status_code == 402


async def test_expired_subscription_blocks_creation_after_grace(
    client: AsyncClient, owner_dsn: str
) -> None:
    admin = await register(client)
    await create_site(client, admin, "Before expiry")
    body = {"name": "New", "installed_capacity_kwp": 5}
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        # Period ended 3 days ago, 7 days of grace: still allowed.
        conn.execute("UPDATE subscriptions SET current_period_end = now() - interval '3 days'")
        assert (
            await client.post(f"{API}/sites", json=body, headers=admin.headers)
        ).status_code == 201
        conn.execute("UPDATE subscriptions SET current_period_end = now() - interval '8 days'")
    expired = await client.post(f"{API}/sites", json=body, headers=admin.headers)
    assert expired.status_code == 402
    assert expired.json()["error"]["code"] == "subscription_expired"
    # Reading existing data stays possible.
    assert (await client.get(f"{API}/sites", headers=admin.headers)).json()["total"] == 2


# role -> (may create site, may create device, may invite, may read audit logs)
MATRIX = {
    "ORGANIZATION_ADMIN": (True, True, True, True),
    "ENERGY_MANAGER": (True, True, False, False),
    "TECHNICAL_MANAGER": (False, True, False, False),
    "TECHNICIAN": (False, False, False, False),
    "VIEWER": (False, False, False, False),
    "GOVERNMENT_AUDITOR": (False, False, False, True),
}


@pytest.mark.parametrize("role", list(MATRIX))
async def test_role_permissions(client: AsyncClient, app: FastAPI, role: str) -> None:
    admin = await register(client)
    site = await create_site(client, admin)
    actor = admin if role == "ORGANIZATION_ADMIN" else await add_member(client, app, admin, role)
    may_site, may_device, may_invite, may_audit = MATRIX[role]

    def expect(allowed: bool, ok: int) -> int:
        return ok if allowed else 403

    # Everyone can read sites.
    assert (await client.get(f"{API}/sites", headers=actor.headers)).status_code == 200
    r = await client.post(
        f"{API}/sites", json={"name": "N", "installed_capacity_kwp": 5}, headers=actor.headers
    )
    assert r.status_code == expect(may_site, 201)
    r = await client.post(
        f"{API}/devices", json={"site_id": site["id"], "name": "D", "kind": "METER"},
        headers=actor.headers,
    )  # fmt: skip
    assert r.status_code == expect(may_device, 201)
    r = await client.post(
        f"{API}/users/invitations", json={"email": "n@example.com", "role": "VIEWER"},
        headers=actor.headers,
    )  # fmt: skip
    assert r.status_code == expect(may_invite, 201)
    r = await client.get(f"{API}/organizations/current/audit-logs", headers=actor.headers)
    assert r.status_code == expect(may_audit, 200)
    r = await client.patch(
        f"{API}/organizations/current", json={"name": "Renamed"}, headers=actor.headers
    )
    assert r.status_code == expect(role == "ORGANIZATION_ADMIN", 200)


async def test_member_cannot_escalate_own_role(client: AsyncClient, app: FastAPI) -> None:
    admin = await register(client)
    viewer = await add_member(client, app, admin, "VIEWER")
    response = await client.patch(
        f"{API}/users/{viewer.user_id}", json={"role": "ORGANIZATION_ADMIN"}, headers=viewer.headers
    )
    assert response.status_code == 403
