"""Cross-tenant access tests: API layer and PostgreSQL Row-Level Security."""

import uuid

import psycopg
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from solarmind_api.models import Organization, Session, Site, User
from tests.helpers import API, create_device, create_gateway, create_site, register

pytestmark = pytest.mark.db


@pytest.fixture
async def two_orgs(client: AsyncClient):  # type: ignore[no-untyped-def]
    a = await register(client, "Org A")
    b = await register(client, "Org B")
    site = await create_site(client, a, "A private site")
    gateway = await create_gateway(client, a, site["id"])
    device = await create_device(client, a, site["id"], gateway_id=gateway["id"])
    asset = (
        await client.post(
            f"{API}/assets",
            json={"site_id": site["id"], "name": "A inverter", "kind": "INVERTER"},
            headers=a.headers,
        )
    ).json()
    return a, b, {"site": site, "gateway": gateway, "device": device, "asset": asset}


async def test_other_org_cannot_read_list_update_or_delete(client: AsyncClient, two_orgs) -> None:  # type: ignore[no-untyped-def]
    a, b, owned = two_orgs
    for collection, key in (("sites", "site"), ("devices", "device")):
        url = f"{API}/{collection}/{owned[key]['id']}"
        assert (await client.get(url, headers=b.headers)).status_code == 404
        assert (
            await client.patch(url, json={"name": "pwned"}, headers=b.headers)
        ).status_code == 404
        assert (await client.delete(url, headers=b.headers)).status_code == 404
        assert (await client.get(f"{API}/{collection}", headers=b.headers)).json()["total"] == 0
    for collection, key in (("assets", "asset"), ("gateways", "gateway")):
        url = f"{API}/{collection}/{owned[key]['id']}"
        assert (await client.delete(url, headers=b.headers)).status_code == 404
    assert (await client.get(f"{API}/gateways", headers=b.headers)).json() == []
    rotate = await client.post(
        f"{API}/gateways/{owned['gateway']['id']}/rotate-key", headers=b.headers
    )
    assert rotate.status_code == 404

    # Org A's data is untouched.
    site = (await client.get(f"{API}/sites/{owned['site']['id']}", headers=a.headers)).json()
    assert site["name"] == "A private site"


async def test_other_org_cannot_attach_to_foreign_rows(client: AsyncClient, two_orgs) -> None:  # type: ignore[no-untyped-def]
    _, b, owned = two_orgs
    # Device on a foreign site.
    response = await client.post(
        f"{API}/devices",
        json={"site_id": owned["site"]["id"], "name": "x", "kind": "INVERTER"},
        headers=b.headers,
    )
    assert response.status_code == 404
    # Own site, but foreign asset / gateway references.
    own_site = await create_site(client, b, "B site")
    for field in ("asset", "gateway"):
        response = await client.post(
            f"{API}/devices",
            json={"site_id": own_site["id"], "name": "x", "kind": "INVERTER",
                  f"{field}_id": owned[field]["id"]},
            headers=b.headers,
        )  # fmt: skip
        assert response.status_code == 404, field


async def test_members_and_audit_logs_are_scoped(client: AsyncClient, two_orgs) -> None:  # type: ignore[no-untyped-def]
    a, b, _ = two_orgs
    members = (await client.get(f"{API}/users", headers=b.headers)).json()
    assert [m["email"] for m in members] == [b.email]
    logs = (await client.get(f"{API}/organizations/current/audit-logs", headers=b.headers)).json()
    assert logs["total"] > 0
    assert a.user_id not in str(logs)
    # Cannot manage a member of another organization.
    response = await client.patch(
        f"{API}/users/{a.user_id}", json={"role": "VIEWER"}, headers=b.headers
    )
    assert response.status_code == 404


async def test_cannot_switch_into_foreign_organization(client: AsyncClient, two_orgs) -> None:  # type: ignore[no-untyped-def]
    a, b, _ = two_orgs
    response = await client.post(
        f"{API}/auth/switch-organization", json={"organization_id": a.org_id}, headers=b.headers
    )
    assert response.status_code == 403
    login = await client.post(
        f"{API}/auth/login",
        json={"email": b.email, "password": "Correct-Horse-42!", "organization_id": a.org_id},
    )
    assert login.status_code == 403


async def test_platform_endpoints_refuse_organization_admins(client: AsyncClient, two_orgs) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = two_orgs
    for path in ("/admin/organizations", "/admin/stats", "/admin/subscriptions"):
        assert (await client.get(f"{API}{path}", headers=a.headers)).status_code == 403


async def test_suspended_organization_loses_access(
    client: AsyncClient,
    two_orgs,
    owner_dsn: str,  # type: ignore[no-untyped-def]
) -> None:
    a, b, _ = two_orgs
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute("UPDATE organizations SET is_active = false WHERE id = %s", (a.org_id,))
    assert (await client.get(f"{API}/sites", headers=a.headers)).status_code == 403
    assert (await client.get(f"{API}/sites", headers=b.headers)).status_code == 200


# --------------------------------------------------------------------------- RLS itself
# These tests deliberately omit the organization filter that services always
# add, to prove the database refuses on its own.


async def test_rls_hides_foreign_rows_without_any_application_filter(
    app: FastAPI,
    two_orgs,  # type: ignore[no-untyped-def]
) -> None:
    a, b, owned = two_orgs
    db = app.state.db
    async with db.tenant(uuid.UUID(b.org_id)) as s:
        assert (await s.scalars(select(Site))).all() == []
        assert [o.id for o in await s.scalars(select(Organization))] == [uuid.UUID(b.org_id)]
        assert [u.email for u in await s.scalars(select(User))] == [b.email]
        # Authentication internals are invisible to tenant transactions.
        assert (await s.scalars(select(Session))).all() == []
        for table in ("devices", "assets", "gateways", "memberships", "subscriptions",
                      "alert_rules", "audit_logs"):  # fmt: skip
            foreign = await s.scalar(
                text(f"SELECT count(*) FROM {table} WHERE organization_id = :o"),  # noqa: S608
                {"o": a.org_id},
            )
            assert foreign == 0, table
    async with db.tenant(uuid.UUID(a.org_id)) as s:
        assert [x.id for x in await s.scalars(select(Site))] == [uuid.UUID(owned["site"]["id"])]


async def test_rls_blocks_writes_into_another_organization(app: FastAPI, two_orgs) -> None:  # type: ignore[no-untyped-def]
    a, b, owned = two_orgs
    db = app.state.db
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db.tenant(uuid.UUID(b.org_id)) as s:
            s.add(Site(organization_id=uuid.UUID(a.org_id), name="x", kind="OTHER",
                       installed_capacity_kwp=1))  # fmt: skip
            await s.flush()
    # UPDATE / DELETE on a foreign row silently affect nothing.
    async with db.tenant(uuid.UUID(b.org_id)) as s:
        result = await s.execute(
            text("UPDATE sites SET name = 'pwned' WHERE id = :i"), {"i": owned["site"]["id"]}
        )
        assert result.rowcount == 0
        result = await s.execute(
            text("DELETE FROM sites WHERE id = :i"), {"i": owned["site"]["id"]}
        )
        assert result.rowcount == 0
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db.tenant(uuid.UUID(b.org_id)) as s:
            await s.execute(
                text("UPDATE sites SET organization_id = :a WHERE organization_id = :b"),
                {"a": a.org_id, "b": b.org_id},
            )
            # (B has no site, so make one first to give the UPDATE a target.)
            s.add(Site(organization_id=uuid.UUID(b.org_id), name="b", kind="OTHER",
                       installed_capacity_kwp=1))  # fmt: skip
            await s.flush()
            await s.execute(
                text("UPDATE sites SET organization_id = :a WHERE organization_id = :b"),
                {"a": a.org_id, "b": b.org_id},
            )


async def test_no_tenant_context_means_no_rows(app: FastAPI, two_orgs) -> None:  # type: ignore[no-untyped-def]
    """A transaction that forgot to declare its organization sees nothing."""
    async with app.state.db.engine.connect() as connection:
        for table in ("sites", "devices", "organizations", "users", "memberships", "sessions"):
            count = await connection.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count == 0, table


async def test_context_does_not_leak_between_pooled_transactions(app: FastAPI, two_orgs) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = two_orgs
    db = app.state.db
    async with db.system() as s:
        assert await s.scalar(text("SELECT count(*) FROM sites")) == 1
    async with db.tenant(uuid.UUID(a.org_id)) as s:
        assert await s.scalar(text("SELECT count(*) FROM sites")) == 1
    # Same pool, new transaction, no context: the previous settings are gone.
    async with db.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM sites")) == 0
        assert await connection.scalar(text("SELECT app_rls_bypass()")) is False


async def test_application_role_is_unprivileged(app: FastAPI, two_orgs) -> None:  # type: ignore[no-untyped-def]
    async with app.state.db.system() as s:
        row = (
            await s.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert tuple(row) == ("solarmind_app", False, False, False, False)
        owned = await s.scalar(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                "AND tableowner = current_user"
            )  # fmt: skip
        )
        assert owned == 0
        unprotected = (
            await s.scalars(
                text(
                    "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity "
                    "AND relname <> 'alembic_version'"
                )  # fmt: skip
            )
        ).all()
        assert unprotected == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE telemetry SET value = 0",
        "DELETE FROM telemetry",
        "UPDATE audit_logs SET action = 'x'",
        "DELETE FROM audit_logs",
        "SELECT * FROM telemetry_hourly",
        "DROP TABLE sites",
        "ALTER TABLE sites DISABLE ROW LEVEL SECURITY",
    ],
)
async def test_application_role_cannot_tamper(app: FastAPI, clean: None, statement: str) -> None:
    with pytest.raises(DBAPIError, match="permission denied|must be owner"):
        async with app.state.db.system() as s:
            await s.execute(text(statement))
