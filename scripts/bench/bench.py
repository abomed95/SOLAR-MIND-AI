"""In-process latency of the main read endpoints (median of N calls).

Run ``seed.py`` first. ``CACHE=1`` enables the Redis read cache; ``TAG``
names the JSON result file written to ``$BENCH_OUT``.
"""

import asyncio
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from tests import conftest  # noqa: E402

DB = "solarmind_bench"
OUT = Path(os.environ.get("BENCH_OUT", "/tmp/solarmind-bench"))  # noqa: S108
N = int(os.environ.get("N", "15"))


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def main(seed: dict[str, str]) -> dict[str, float]:
    from solarmind_api.main import create_app

    cache = (
        {"cache_ttl_overview_s": 60, "cache_ttl_insights_s": 300} if os.environ.get("CACHE") else {}
    )
    app = create_app(conftest.make_settings(
        database_url=SecretStr(conftest.APP_URL.replace("solarmind_test", DB)), **cache
    ))  # fmt: skip
    headers = {"Authorization": f"Bearer {seed['token']}"}
    now = datetime.now(UTC)
    series = f"/api/v1/telemetry/series?site_id={seed['site']}&metric=pv_power_kw&end={iso(now)}"
    cases = {
        "auth/me": "/api/v1/auth/me",
        "telemetry/latest": "/api/v1/telemetry/latest",
        "series 24h (raw)": f"{series}&start={iso(now - timedelta(hours=24))}",
        "series 30d (hourly)": f"{series}&start={iso(now - timedelta(days=30))}",
        "energy/overview": "/api/v1/energy/overview",
        "ai/insights/site": f"/api/v1/ai/insights/sites/{seed['site']}",
    }
    results = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench") as client:
        for name, url in cases.items():
            warm = await client.get(url, headers=headers)
            if warm.status_code != 200:
                raise RuntimeError(f"{name}: {warm.status_code} {warm.text[:300]}")
            timings = []
            for _ in range(N):
                started = time.perf_counter()
                await client.get(url, headers=headers)
                timings.append((time.perf_counter() - started) * 1000)
            results[name] = round(statistics.median(timings), 1)
    await app.state.db.dispose()
    await app.state.redis.aclose()
    return results


if __name__ == "__main__":
    measured = asyncio.run(main(json.loads((OUT / "seed.json").read_text())[0]))
    for case, ms in measured.items():
        print(f"{case:22s} {ms:8.1f} ms")
    (OUT / f"{os.environ.get('TAG', 'run')}.json").write_text(json.dumps(measured))
