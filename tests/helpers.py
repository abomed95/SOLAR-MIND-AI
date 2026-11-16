"""Small helpers shared by the integration and security tests."""

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

PASSWORD = "Correct-Horse-42!"
API = "/api/v1"


@dataclass
class Actor:
    headers: dict[str, str]
    user_id: str
    org_id: str
    email: str
    token: str


def _actor(body: dict[str, Any]) -> Actor:
    return Actor(
        headers={"Authorization": f"Bearer {body['token']}"},
        user_id=body["user"]["id"],
        org_id=body["organization"]["id"],
        email=body["user"]["email"],
        token=body["token"],
    )


async def register(
    client: AsyncClient, org_name: str = "Acme Solar", email: str | None = None
) -> Actor:
    email = email or f"admin-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        f"{API}/auth/register",
        json={"organization_name": org_name, "full_name": "Admin", "email": email,
              "password": PASSWORD},
    )  # fmt: skip
    assert response.status_code == 201, response.text
    return _actor(response.json())


async def add_member(client: AsyncClient, app: FastAPI, admin: Actor, role: str) -> Actor:
    """Invite a user with ``role`` and accept the invitation (the real flow)."""
    email = f"{role.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        f"{API}/users/invitations", json={"email": email, "role": role}, headers=admin.headers
    )
    assert response.status_code == 201, response.text
    token = response.json()["invitation_url"].split("token=")[1]
    response = await client.post(
        f"{API}/auth/invitations/accept",
        json={"token": token, "full_name": role.title(), "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return _actor(response.json())


async def create_site(
    client: AsyncClient, actor: Actor, name: str = "Site A", **extra: Any
) -> dict[str, Any]:
    payload = {"name": name, "installed_capacity_kwp": 100.0, "battery_capacity_kwh": 200.0,
               "tariff_per_kwh": "55", **extra}  # fmt: skip
    response = await client.post(f"{API}/sites", json=payload, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def create_device(client: AsyncClient, actor: Actor, site_id: str, name: str = "Inverter 1",
                        **extra: Any) -> dict[str, Any]:  # fmt: skip
    payload = {"site_id": site_id, "name": name, "kind": "INVERTER", **extra}
    response = await client.post(f"{API}/devices", json=payload, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def create_gateway(client: AsyncClient, actor: Actor, site_id: str) -> dict[str, Any]:
    response = await client.post(
        f"{API}/gateways", json={"site_id": site_id, "name": "Gateway 1"}, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]
