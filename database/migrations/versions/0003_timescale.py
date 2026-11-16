"""TimescaleDB: hypertable, hourly continuous aggregate, retention.

Runs BEFORE Row-Level Security is enabled (0004): TimescaleDB refuses to create
a continuous aggregate on a hypertable that already has row security, because
the aggregate itself cannot carry policies. Tenant isolation on the aggregate
is restored in 0004 by a filtering view, the only object the application role
may read.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RAW_RETENTION = "730 days"


def upgrade() -> None:
    op.execute(
        "SELECT create_hypertable('telemetry', 'time', chunk_time_interval => INTERVAL '7 days')"
    )
    with op.get_context().autocommit_block():
        # Real-time aggregate (materialized_only = false): buckets not yet
        # materialized are computed on the fly, so reads are never stale.
        op.execute(
            """
            CREATE MATERIALIZED VIEW telemetry_hourly
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket(INTERVAL '1 hour', time) AS bucket,
                   organization_id, site_id, device_id, metric_name,
                   avg(value) AS avg_value,
                   min(value) AS min_value,
                   max(value) AS max_value,
                   count(*)   AS samples
            FROM telemetry
            WHERE quality = 'valid'
            GROUP BY bucket, organization_id, site_id, device_id, metric_name
            WITH NO DATA
            """
        )
    op.execute(
        """
        SELECT add_continuous_aggregate_policy('telemetry_hourly',
            start_offset => INTERVAL '3 days',
            end_offset => INTERVAL '1 hour',
            schedule_interval => INTERVAL '30 minutes')
        """
    )
    op.execute(f"SELECT add_retention_policy('telemetry', INTERVAL '{RAW_RETENTION}')")


def downgrade() -> None:
    op.execute("SELECT remove_retention_policy('telemetry', if_exists => true)")
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS telemetry_hourly")
