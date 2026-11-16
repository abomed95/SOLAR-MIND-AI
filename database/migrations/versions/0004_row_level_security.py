"""Row-Level Security, append-only grants, tenant view over the hourly aggregate.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

from solarmind_api.models import SYSTEM_TABLES, TENANT_TABLES

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "solarmind_app"


def upgrade() -> None:
    # --- tenant context helpers (read the transaction-local settings) ---
    op.execute(
        """
        CREATE FUNCTION app_rls_bypass() RETURNS boolean LANGUAGE sql STABLE AS
        $$ SELECT current_setting('app.rls_bypass', true) = 'on' $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_current_org() RETURNS uuid LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.current_org_id', true), '')::uuid $$;
        """
    )

    # --- Row-Level Security ---
    # ENABLE without FORCE: the owner (migrations, TimescaleDB background jobs)
    # is exempt, the application role never is.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (app_rls_bypass() OR organization_id = app_current_org())
            WITH CHECK (app_rls_bypass() OR organization_id = app_current_org())
            """
        )
    for table in SYSTEM_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY system_only ON {table} "
            f"USING (app_rls_bypass()) WITH CHECK (app_rls_bypass())"
        )
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY own_organization ON organizations
        USING (app_rls_bypass() OR id = app_current_org())
        WITH CHECK (app_rls_bypass() OR id = app_current_org())
        """
    )
    # A user row is visible only to members of an organization it belongs to.
    # The sub-select is itself filtered by the memberships policy.
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY organization_members ON users
        USING (app_rls_bypass() OR id IN (SELECT user_id FROM memberships))
        WITH CHECK (app_rls_bypass())
        """
    )

    # --- append-only tables ---
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON telemetry FROM {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM {APP_ROLE}")

    # --- hourly aggregate: tenant-filtered access only ---
    # The continuous aggregate cannot carry RLS policies. The application role
    # gets no privilege on it and reads through this view instead, which runs
    # with its owner's rights and applies the same tenant predicate.
    op.execute(f"REVOKE ALL ON telemetry_hourly FROM {APP_ROLE}")
    op.execute(
        """
        CREATE VIEW telemetry_hourly_tenant WITH (security_barrier) AS
        SELECT * FROM telemetry_hourly
        WHERE app_rls_bypass() OR organization_id = app_current_org()
        """
    )
    op.execute(f"GRANT SELECT ON telemetry_hourly_tenant TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS telemetry_hourly_tenant")
    for table in [*TENANT_TABLES, *SYSTEM_TABLES, "organizations", "users"]:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        for policy in ("tenant_isolation", "system_only", "own_organization", "organization_members"):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS app_rls_bypass()")
    op.execute("DROP FUNCTION IF EXISTS app_current_org()")
