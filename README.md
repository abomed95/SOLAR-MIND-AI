# SOLAR MIND AI

Solar monitoring, predictive maintenance and energy intelligence platform.
Multi-tenant B2B/B2G SaaS by **AMD Digital** (Djibouti).

> **Status: Phase 1 (foundation), in progress.** What exists today is listed
> below. Everything else in the roadmap is *not built yet*.

## What works today

| Component | State |
|-----------|-------|
| API skeleton (FastAPI, typed settings, JSON logs) | Done, tested |
| `GET /api/v1/health/live` and `/ready` (real DB + Redis probes, 503 on failure) | Done, tested |
| Docker Compose: `api`, `postgres` (TimescaleDB 2.17 / PG 16), `redis`, `mqtt` | Done, verified |
| Web app (Next.js), worker, simulator, object storage, reverse proxy | Not started |
| Database schema, migrations, auth, tenancy, IoT, analytics, AI | Not started |

## Quick start

```bash
cp .env.example .env        # then set POSTGRES_PASSWORD
docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health/ready
```

OpenAPI docs: <http://127.0.0.1:8000/api/docs> (disabled when
`SOLARMIND_ENVIRONMENT=production`).

## Tests

```bash
cd apps/api
uv sync
uv run pytest -m "not integration"     # no service needed
uv run ruff check . && uv run mypy src
```

The `integration` test needs PostgreSQL and Redis reachable through
`SOLARMIND_DATABASE_URL` and `SOLARMIND_REDIS_URL`.

## Repository layout

```
apps/api/            FastAPI application (modular monolith)
apps/web/            Next.js front end            (planned)
apps/worker/         background jobs              (planned)
apps/simulator/      solar plant simulator        (planned)
packages/            energy-core, device-adapters, ai-agents, shared-schemas (planned)
infrastructure/      mqtt, monitoring, deployment
database/            migrations, seeds
docs/                architecture, api, deployment, security, testing
```

Architecture and decisions: [`docs/architecture/system-design.md`](docs/architecture/system-design.md).
Latest performance and security audit:
[`docs/audit/2026-11-audit-performance-securite.md`](docs/audit/2026-11-audit-performance-securite.md)
(benchmark: `scripts/bench/`).
