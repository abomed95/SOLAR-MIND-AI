"""Anomaly detection on ingested data and the alert lifecycle."""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from solarmind_api.services import alerts as alert_service
from solarmind_api.services import notifications
from tests.helpers import API, add_member, create_device, create_gateway, create_site, register

pytestmark = pytest.mark.db
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def plant(client: AsyncClient):  # type: ignore[no-untyped-def]
    admin = await register(client)
    site = await create_site(client, admin)
    gateway = await create_gateway(client, admin, site["id"])
    inverter = await create_device(client, admin, site["id"], "Inverter A")
    return {"admin": admin, "site": site, "key": gateway["api_key"], "inverter": inverter["id"]}


async def send(client: AsyncClient, plant: dict, minutes_ago: int, **metrics: float) -> None:  # type: ignore[type-arg]
    response = await client.post(
        f"{API}/ingestion/batch",
        json={"records": [{"device_id": plant["inverter"], "metrics": metrics,
                           "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat()}]},
        headers={"X-Gateway-Key": plant["key"]},
    )  # fmt: skip
    assert response.status_code == 200, response.text


async def alerts(client: AsyncClient, plant: dict, **params: Any) -> list[dict]:  # type: ignore[type-arg]
    response = await client.get(f"{API}/alerts", params=params, headers=plant["admin"].headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]  # type: ignore[no-any-return]


async def test_threshold_violation_raises_an_alert_with_evidence(
    client: AsyncClient,
    plant: dict,  # type: ignore[type-arg]
) -> None:
    await send(client, plant, 10, inverter_temperature_c=60.0)  # normal
    assert await alerts(client, plant) == []

    await send(client, plant, 5, inverter_temperature_c=80.5)
    [alert] = await alerts(client, plant)
    assert (alert["title"], alert["severity"], alert["status"]) == (
        "Inverter temperature high", "HIGH", "OPEN")  # fmt: skip
    assert alert["category"] == "TEMPERATURE" and alert["device_id"] == plant["inverter"]
    assert alert["evidence"] == {
        "kind": "threshold", "metric": "inverter_temperature_c", "value": 80.5, "unit": "degC",
        "operator": "GT", "threshold": 75.0, "source": "MEASURED", "violations_in_batch": 1,
        "measured_at": (NOW - timedelta(minutes=5)).isoformat(),
    }  # fmt: skip
    assert "80.50 degC" in alert["message"] and "threshold > 75" in alert["message"]


async def test_repeated_violations_are_deduplicated(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    for minutes_ago, value in ((20, 78.0), (15, 79.0), (10, 81.0)):
        await send(client, plant, minutes_ago, inverter_temperature_c=value)
    [alert] = await alerts(client, plant)
    assert alert["occurrence_count"] == 3
    assert alert["evidence"]["value"] == 81.0  # evidence follows the latest measurement
    assert alert["first_seen_at"] < alert["last_seen_at"]

    # Crossing the higher threshold opens a second, more severe alert.
    await send(client, plant, 5, inverter_temperature_c=90.0)
    by_severity = {a["severity"]: a for a in await alerts(client, plant)}
    assert set(by_severity) == {"HIGH", "CRITICAL"}
    assert by_severity["HIGH"]["occurrence_count"] == 4

    # Replaying an already stored measurement changes nothing.
    await send(client, plant, 5, inverter_temperature_c=90.0)
    assert {a["severity"]: a["occurrence_count"] for a in await alerts(client, plant)} == {
        "HIGH": 4, "CRITICAL": 1}  # fmt: skip


async def test_flagged_values_do_not_trigger_alerts(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    await send(client, plant, 5, inverter_temperature_c=5000.0)  # out of physical range
    assert await alerts(client, plant) == []


async def test_alert_lifecycle_history_and_reopening(
    client: AsyncClient,
    app: FastAPI,
    plant: dict,  # type: ignore[type-arg]
) -> None:
    admin = plant["admin"]
    technician = await add_member(client, app, admin, "TECHNICIAN")
    await send(client, plant, 10, inverter_temperature_c=80.0)
    [alert] = await alerts(client, plant)
    url = f"{API}/alerts/{alert['id']}"

    assigned = await client.post(f"{url}/assign", json={"user_id": technician.user_id},
                                 headers=admin.headers)  # fmt: skip
    assert assigned.json()["assigned_to"] == technician.user_id
    acknowledged = await client.post(f"{url}/acknowledge", headers=technician.headers)
    assert acknowledged.json()["status"] == "ACKNOWLEDGED"
    assert acknowledged.json()["acknowledged_by"] == technician.user_id
    again = await client.post(f"{url}/acknowledge", headers=technician.headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_transition"

    # Still violating while acknowledged: folded into the same alert.
    await send(client, plant, 8, inverter_temperature_c=82.0)
    assert len(await alerts(client, plant)) == 1

    resolved = await client.post(f"{url}/resolve", json={"note": "Fan replaced"},
                                 headers=technician.headers)  # fmt: skip
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["resolution_note"] == "Fan replaced"
    assert (await client.post(f"{url}/resolve", json={}, headers=admin.headers)).status_code == 409

    events = (await client.get(f"{url}/events", headers=admin.headers)).json()
    assert [e["event"] for e in events] == ["CREATED", "ASSIGNED", "ACKNOWLEDGED", "RESOLVED"]
    assert events[-1]["note"] == "Fan replaced" and events[-1]["actor_id"] == technician.user_id

    # The problem comes back after resolution: a NEW alert, history preserved.
    await send(client, plant, 2, inverter_temperature_c=83.0)
    assert len(await alerts(client, plant, active="true")) == 1
    assert len(await alerts(client, plant, status="RESOLVED")) == 1
    assert len(await alerts(client, plant)) == 2


async def test_alert_permissions_and_isolation(
    client: AsyncClient, app: FastAPI, plant: dict
) -> None:  # type: ignore[type-arg]
    admin = plant["admin"]
    viewer = await add_member(client, app, admin, "VIEWER")
    technician = await add_member(client, app, admin, "TECHNICIAN")
    await send(client, plant, 5, inverter_temperature_c=80.0)
    [alert] = await alerts(client, plant)
    url = f"{API}/alerts/{alert['id']}"

    assert (await client.get(url, headers=viewer.headers)).status_code == 200
    assert (await client.post(f"{url}/acknowledge", headers=viewer.headers)).status_code == 403
    assign = await client.post(f"{url}/assign", json={"user_id": viewer.user_id},
                               headers=technician.headers)  # fmt: skip
    assert assign.status_code == 403  # technicians act on alerts, they do not dispatch them

    intruder = await register(client, "Intruder")
    assert (await client.get(url, headers=intruder.headers)).status_code == 404
    assert (
        await client.post(f"{url}/resolve", json={}, headers=intruder.headers)
    ).status_code == 404
    assert (await client.get(f"{url}/events", headers=intruder.headers)).status_code == 404
    assert (await client.get(f"{API}/alerts", headers=intruder.headers)).json()["total"] == 0
    # Assigning to someone outside the organization is refused.
    foreign = await client.post(f"{url}/assign", json={"user_id": intruder.user_id},
                                headers=admin.headers)  # fmt: skip
    assert foreign.status_code == 404


async def test_configurable_rules(client: AsyncClient, plant: dict) -> None:  # type: ignore[type-arg]
    admin = plant["admin"]
    rules = (await client.get(f"{API}/alerts/rules", headers=admin.headers)).json()
    assert len(rules) == 4  # defaults created with the organization
    high = next(r for r in rules if r["name"] == "Inverter temperature high")

    # Raise the threshold: 80 degC no longer alerts.
    patched = await client.patch(f"{API}/alerts/rules/{high['id']}", json={"threshold": 82},
                                 headers=admin.headers)  # fmt: skip
    assert patched.json()["threshold"] == 82
    await send(client, plant, 10, inverter_temperature_c=80.0)
    assert await alerts(client, plant) == []

    created = await client.post(f"{API}/alerts/rules", headers=admin.headers, json={
        "name": "Low production at noon", "metric_name": "pv_power_kw", "operator": "LT",
        "threshold": 5, "severity": "WARNING", "category": "PRODUCTION",
        "site_id": plant["site"]["id"]})  # fmt: skip
    assert created.status_code == 201
    await send(client, plant, 5, pv_power_kw=2.0)
    [alert] = await alerts(client, plant)
    assert (alert["title"], alert["severity"]) == ("Low production at noon", "WARNING")

    # Disabled rules are not evaluated.
    await client.patch(f"{API}/alerts/rules/{created.json()['id']}", json={"enabled": False},
                       headers=admin.headers)  # fmt: skip
    await client.post(f"{API}/alerts/{alert['id']}/resolve", json={}, headers=admin.headers)
    await send(client, plant, 2, pv_power_kw=1.0)
    assert await alerts(client, plant, active="true") == []

    bad = await client.post(f"{API}/alerts/rules", headers=admin.headers, json={
        "name": "x", "metric_name": "not_a_metric", "operator": "GT", "threshold": 1,
        "severity": "INFO"})  # fmt: skip
    assert bad.status_code == 422


async def test_offline_device_detection_and_auto_resolution(
    client: AsyncClient,
    app: FastAPI,
    plant: dict,  # type: ignore[type-arg]
) -> None:
    await send(client, plant, 60, pv_power_kw=10.0)  # last heard an hour ago (interval 5 min)
    async with app.state.db.system() as s:
        assert await alert_service.detect_offline_devices(s) == (1, 0)
    async with app.state.db.system() as s:
        assert await alert_service.detect_offline_devices(s) == (0, 0)  # deduplicated

    [alert] = await alerts(client, plant)
    assert (alert["category"], alert["severity"]) == ("COMMUNICATION", "HIGH")
    assert alert["evidence"]["kind"] == "communication_loss"
    assert "unknown, not zero" in alert["message"]

    await send(client, plant, 1, pv_power_kw=12.0)  # the device reports again
    async with app.state.db.system() as s:
        assert await alert_service.detect_offline_devices(s) == (0, 1)
    [resolved] = await alerts(client, plant)
    assert resolved["status"] == "RESOLVED" and "automatically" in resolved["resolution_note"]
    events = (await client.get(f"{API}/alerts/{alert['id']}/events",
                               headers=plant["admin"].headers)).json()  # fmt: skip
    assert [e["event"] for e in events] == ["CREATED", "AUTO_RESOLVED"]


async def test_notifications_preferences_and_delivery_retries(
    client: AsyncClient,
    app: FastAPI,
    plant: dict,
    owner_dsn: str,  # type: ignore[type-arg]
) -> None:
    admin = plant["admin"]
    quiet = await add_member(client, app, admin, "VIEWER")
    prefs = await client.put(f"{API}/notifications/preferences", headers=quiet.headers, json={
        "email_enabled": True, "min_severity": "CRITICAL"})  # fmt: skip
    assert prefs.status_code == 200
    invalid = await client.put(f"{API}/notifications/preferences", headers=quiet.headers, json={
        "whatsapp_enabled": True})  # fmt: skip
    assert invalid.status_code == 422
    await client.put(f"{API}/notifications/preferences", headers=admin.headers, json={
        "email_enabled": True, "whatsapp_enabled": True, "whatsapp_number": "+25377000000",
        "min_severity": "WARNING"})  # fmt: skip

    await send(client, plant, 5, inverter_temperature_c=80.0)  # HIGH alert

    # In-app: every member who can read alerts, whatever their e-mail threshold.
    for actor in (admin, quiet):
        inbox = (await client.get(f"{API}/notifications", headers=actor.headers)).json()
        assert inbox["total"] == 1 and inbox["items"][0]["severity"] == "HIGH"
    await client.post(f"{API}/notifications/read-all", headers=admin.headers)
    unread = await client.get(f"{API}/notifications?unread=true", headers=admin.headers)
    assert unread.json()["total"] == 0

    with psycopg.connect(owner_dsn) as conn:
        queued = conn.execute(
            "SELECT channel, recipient FROM notification_deliveries ORDER BY channel"
        ).fetchall()
    # The viewer asked for CRITICAL only: nothing queued for a HIGH alert.
    assert queued == [("EMAIL", admin.email), ("WHATSAPP", "+25377000000")]

    class FlakyEmail:
        calls = 0

        async def send(self, recipient: str, subject: str, body: str) -> None:
            FlakyEmail.calls += 1
            if FlakyEmail.calls == 1:
                raise ConnectionError("smtp timeout")

    providers = {"EMAIL": FlakyEmail(), "WHATSAPP": notifications.WhatsAppNotConfigured()}
    settings = app.state.settings
    first = await notifications.dispatch_pending(app.state.db, settings, providers)
    # E-mail failed once and is rescheduled; WhatsApp has no provider and fails honestly.
    assert first == {"sent": 0, "retry": 1, "failed": 1}
    assert await notifications.dispatch_pending(app.state.db, settings, providers) == {
        "sent": 0, "retry": 0, "failed": 0}  # not due yet (backoff)  # fmt: skip
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute("UPDATE notification_deliveries SET next_attempt_at = now() "
                     "WHERE status = 'PENDING'")  # fmt: skip
    assert (await notifications.dispatch_pending(app.state.db, settings, providers))["sent"] == 1
    with psycopg.connect(owner_dsn) as conn:
        rows = conn.execute(
            "SELECT channel, status, attempts, last_error FROM notification_deliveries "
            "ORDER BY channel"
        ).fetchall()
    assert rows == [("EMAIL", "SENT", 2, None),
                    ("WHATSAPP", "FAILED", 1, "WhatsApp provider is not configured")]  # fmt: skip


async def test_delivery_gives_up_after_max_attempts(app: FastAPI, client: AsyncClient, plant: dict,
                                                    owner_dsn: str) -> None:  # type: ignore[type-arg]  # fmt: skip
    await send(client, plant, 5, inverter_temperature_c=80.0)

    class AlwaysDown:
        async def send(self, recipient: str, subject: str, body: str) -> None:
            raise ConnectionError("down")

    providers = {"EMAIL": AlwaysDown(), "WHATSAPP": AlwaysDown()}
    settings = app.state.settings.model_copy(update={"notification_max_attempts": 3})
    outcomes = []
    for _ in range(3):
        outcomes.append(await notifications.dispatch_pending(app.state.db, settings, providers))
        with psycopg.connect(owner_dsn, autocommit=True) as conn:
            conn.execute("UPDATE notification_deliveries SET next_attempt_at = now()")
    assert [o["retry"] for o in outcomes] == [1, 1, 0] and outcomes[-1]["failed"] == 1
    assert notifications.backoff(1).total_seconds() == 30
    assert notifications.backoff(4).total_seconds() == 240
    assert notifications.backoff(50).total_seconds() == 3600
