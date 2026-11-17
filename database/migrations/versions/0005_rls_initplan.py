"""RLS policies evaluated once per statement instead of once per row.

The tenant predicates of 0004 call ``app_rls_bypass()`` and
``app_current_org()``. Both are inlinable SQL functions, so PostgreSQL expands
them into ``current_setting(...)`` calls evaluated **for every row** read. On
telemetry this cost about 20 % of the query time (EXPLAIN ANALYZE, 3.1 M rows,
see docs/audit/2026-11-audit-performance-securite.md).

Wrapping each call in a scalar sub-select turns it into an InitPlan: computed
once per statement, then compared as a constant. The semantics are unchanged.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

from solarmind_api.models import SYSTEM_TABLES, TENANT_TABLES

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

BYPASS = "(SELECT app_rls_bypass())"
ORG = "(SELECT app_current_org())"


def _policies(bypass: str, org: str) -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({bypass} OR organization_id = {org}) "
            f"WITH CHECK ({bypass} OR organization_id = {org})"
        )
    for table in SYSTEM_TABLES:
        op.execute(f"DROP POLICY system_only ON {table}")
        op.execute(f"CREATE POLICY system_only ON {table} USING ({bypass}) WITH CHECK ({bypass})")
    op.execute("DROP POLICY own_organization ON organizations")
    op.execute(
        f"CREATE POLICY own_organization ON organizations "
        f"USING ({bypass} OR id = {org}) WITH CHECK ({bypass} OR id = {org})"
    )
    op.execute("DROP POLICY organization_members ON users")
    op.execute(
        f"CREATE POLICY organization_members ON users "
        f"USING ({bypass} OR id IN (SELECT user_id FROM memberships)) WITH CHECK ({bypass})"
    )
    op.execute(
        f"CREATE OR REPLACE VIEW telemetry_hourly_tenant WITH (security_barrier) AS "
        f"SELECT * FROM telemetry_hourly WHERE {bypass} OR organization_id = {org}"
    )


def upgrade() -> None:
    _policies(BYPASS, ORG)


def downgrade() -> None:
    _policies("app_rls_bypass()", "app_current_org()")
