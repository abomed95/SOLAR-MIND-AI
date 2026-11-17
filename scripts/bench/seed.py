"""Seed a benchmark database (``solarmind_bench``) through the real API.

3 organizations, 1 site each, 20 inverters per site, 6 metrics sampled every
5 minutes for 30 days: about 3.1 million telemetry rows.

    POSTGRES_USER=... POSTGRES_PASSWORD=... SOLARMIND_APP_DB_PASSWORD=... \\
        uv run python scripts/bench/seed.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from tests import conftest  # noqa: E402
from tests.helpers import create_device, create_site, register  # noqa: E402

DB = "solarmind_bench"
OUT = Path(os.environ.get("BENCH_OUT", "/tmp/solarmind-bench"))  # noqa: S108
METRICS = ["pv_power_kw", "irradiance_w_m2", "inverter_temperature_c", "load_power_kw",
           "grid_import_kw", "battery_soc_percent"]  # fmt: skip


def migrate() -> None:
    with psycopg.connect(f"{conftest.OWNER_DSN}/postgres", autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DB}")
    owner = conftest.OWNER_DSN.replace("postgresql://", "postgresql+psycopg://")
    os.environ["SOLARMIND_MIGRATION_DATABASE_URL"] = f"{owner}/{DB}"
    command.upgrade(Config(str(ROOT / "database" / "alembic.ini")), "head")


async def main() -> list[dict[str, object]]:
    from solarmind_api.main import create_app

    migrate()
    app = create_app(
        conftest.make_settings(
            database_url=SecretStr(conftest.APP_URL.replace("solarmind_test", DB))
        )
    )
    app.state.mailer = conftest.CapturingMailer()
    orgs = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench") as client:
        for i in range(3):
            actor = await register(client, f"Org {i}")
            site = await create_site(client, actor, f"Site {i}")
            devices = [
                await create_device(client, actor, site["id"], f"Inv {j}") for j in range(20)
            ]
            orgs.append({"token": actor.token, "org": actor.org_id, "site": site["id"],
                         "devices": [d["id"] for d in devices]})  # fmt: skip
    await app.state.db.dispose()
    await app.state.redis.aclose()

    with psycopg.connect(f"{conftest.OWNER_DSN}/{DB}", autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO telemetry (time, device_id, metric_name, organization_id, site_id, value,
                                   quality, source)
            SELECT t, d.id, m, d.organization_id, d.site_id, random() * 100, 'valid', 'SIMULATED'
            FROM devices d,
                 generate_series(now() - interval '30 days', now(), interval '5 minutes') t,
                 unnest(CAST(%s AS text[])) m
            """,
            (METRICS,),
        )
        conn.execute("CALL refresh_continuous_aggregate('telemetry_hourly', NULL, NULL)")
        conn.execute("VACUUM ANALYZE")
        rows = conn.execute("SELECT count(*) FROM telemetry").fetchone()
    print(f"telemetry rows: {rows[0] if rows else 0}")
    return orgs


if __name__ == "__main__":
    seeded = asyncio.run(main())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "seed.json").write_text(json.dumps(seeded))
