"""The demo seeding goes through the public API only: a full end-to-end pass."""

import json
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from solarmind_simulator.__main__ import seed
from tests.helpers import API

pytestmark = pytest.mark.db


async def test_seed_creates_demo_tenants_with_simulated_history(
    app: FastAPI, client: AsyncClient, owner_dsn: str, tmp_path: Path
) -> None:
    out = tmp_path / "simulator.json"
    await seed("http://test", out, days=3, interval_s=300, password="Demo-Password-123",
               transport=ASGITransport(app=app))  # fmt: skip

    config = json.loads(out.read_text())
    assert len(config["plants"]) == 5
    assert oct(out.stat().st_mode)[-3:] == "600"  # holds gateway keys

    with psycopg.connect(owner_dsn) as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # type: ignore[index]  # noqa: E731
        assert one("SELECT count(*) FROM organizations") == 3
        assert one("SELECT count(*) FROM sites WHERE is_simulated") == 5
        assert one("SELECT count(*) FROM devices WHERE NOT is_simulated") == 0
        # Every stored value is labelled as simulated.
        assert one("SELECT count(*) FROM telemetry") > 40_000
        assert one("SELECT count(*) FROM telemetry WHERE source <> 'SIMULATED'") == 0
        # 3 days x 288 samples for a healthy inverter.
        assert (
            one(
                "SELECT count(*) FROM telemetry t JOIN sites s ON s.id = t.site_id "
                "WHERE s.name = 'Logistics Warehouse' AND metric_name = 'pv_power_kw'"
            )
            == 864
        )
        # The injected over-temperature fault raised alerts through the normal rule engine.
        alerts = conn.execute(
            "SELECT a.title, a.severity FROM alerts a JOIN sites s ON s.id = a.site_id "
            "WHERE s.name = 'Seaside Hotel' ORDER BY a.severity"
        ).fetchall()
        assert ("Inverter temperature critical", "CRITICAL") in alerts
        # The communication-loss fault left a real gap (no rows), not zeros.
        gap = one(
            "SELECT count(*) FROM telemetry t JOIN sites s ON s.id = t.site_id "
            "WHERE s.name = 'Cold Storage Plant' AND metric_name = 'pv_power_kw'"
        )
        assert gap == 864 - 24

    # Seeding again is a no-op (configuration already present).
    await seed("http://test", out, days=3, interval_s=300, password="Demo-Password-123",
               transport=ASGITransport(app=app))  # fmt: skip
    login = await client.post(f"{API}/auth/login", json={
        "email": "admin@hotel.demo.solarmind.example", "password": "Demo-Password-123"})  # fmt: skip
    sites = await client.get(
        f"{API}/sites", headers={"Authorization": f"Bearer {login.json()['token']}"}
    )
    assert [s["name"] for s in sites.json()["items"]] == ["City Hotel", "Seaside Hotel"]
