# System design

Living document. Sections marked *(planned)* describe intent, not code.

## 1. Overview

```
 Edge gateway (site)                         Cloud
 +-------------------------+     MQTT/TLS    +-----------+
 | device adapters (R/O)   | --------------> | Mosquitto |--+
 | Modbus TCP/RTU, mock    |   REST batch    +-----------+  |
 | SQLite outbox buffer    | ------------------------------>|
 +-------------------------+                                v
                                              +-----------------------------+
   Browser --> reverse proxy --> Next.js -->  | API (FastAPI, modular       |
                                              | monolith) + worker          |
                                              +--------------+--------------+
                                                             |
                           +---------------+-----------------+----------------+
                           v               v                                  v
                   PostgreSQL + TimescaleDB (RLS)        Redis          S3-compatible
                   business data + telemetry        queue, cache,       storage (PDF,
                                                    rate limiting       attachments)
```

## 2. Module boundaries *(planned unless stated)*

The API is one deployable with strict internal modules. A module exposes a
service layer; other modules never import its tables or repositories.

| Module | Responsibility |
|--------|----------------|
| `identity` | users, sessions, MFA, invitations, password reset |
| `tenancy` | organizations, memberships, roles, permissions, RLS context |
| `assets` | sites, assets, devices, gateways |
| `ingestion` | MQTT and REST batch intake, validation, idempotency |
| `telemetry` | raw storage, queries, continuous aggregates |
| `energy` | adapter over `packages/energy-core` (pure calculations) |
| `alerts` | thresholds, deduplication, lifecycle, notifications |
| `maintenance` | tickets (CMMS) |
| `reports` | PDF generation |
| `billing` | plans, entitlements, payment provider abstraction |
| `ai` | agent orchestration, LLM abstraction, tool registry |
| `health` | liveness and readiness (**implemented**) |

Pure logic lives in `packages/` with no web or database dependency, so it can
be unit-tested alone and reused by the worker and the edge gateway:
`energy-core`, `device-adapters`, `ai-agents`, `shared-schemas`.

## 3. Decisions

### ADR-001 Modular monolith
One API process plus one worker process sharing the same code base. Splitting
into services is deferred until a module has a proven independent scaling or
release need.

### ADR-002 Tenant isolation in two layers
Every tenant-owned row carries `organization_id`. Layer 1: the service layer
always filters on the caller's organization. Layer 2: PostgreSQL Row-Level
Security, with the organization set per transaction
(`SET LOCAL app.current_org_id`). The application connects with a role that is
**not** the table owner and has no `BYPASSRLS`, otherwise RLS is silently
skipped. Cross-tenant tests run against real PostgreSQL, not mocks.

### ADR-003 Telemetry storage: narrow hypertable
One row per `(time, device_id, metric_name)` with a numeric value, a quality
flag and a source flag (`MEASURED` or `SIMULATED`). A narrow layout accepts new
metrics without migrations. Idempotency comes from a unique key on
`(device_id, metric_name, time)` and `ON CONFLICT DO NOTHING`. Raw rows are
never updated; derived values live in continuous aggregates or separate tables.

### ADR-004 Calculations are deterministic and separate from AI
`energy-core` owns every formula. The LLM only receives results through typed
tools, each scoped to the caller's organization. No SQL access, no write tool
towards physical equipment. Each value is labelled measured, calculated,
estimated or AI-generated.

### ADR-005 Readiness fails closed (implemented)
`/api/v1/health/ready` runs a real `SELECT 1` and a Redis `PING`, each bounded
by `SOLARMIND_READINESS_TIMEOUT_S`. Any failure or timeout returns 503. The
failure reason is logged, never returned, since connection errors can reveal
internal hostnames.

### ADR-006 Configuration without defaults for secrets (implemented)
Settings are read from `SOLARMIND_*` variables. Connection strings have no
default and are typed `SecretStr`: a missing value stops startup, and values do
not appear in `repr` or logs.

### ADR-007 LLM provider behind an interface *(planned)*
The assistant depends on an `LLMProvider` protocol. A deterministic provider
(no network) is the default for tests and for operation without credentials.

## 4. Known gaps

- The development MQTT broker allows anonymous clients; its port is not
  published. Per-gateway credentials, ACLs and TLS come with Phase 3.
- No real Modbus device has been tested. Hardware integration must not be
  described as complete until one is.
