"""Baseline: TimescaleDB extension and the application role.

Revision ID: 0001
Revises:
"""
import os

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "solarmind_app"


def upgrade() -> None:
    password = os.environ.get("SOLARMIND_APP_DB_PASSWORD")
    if not password:
        raise RuntimeError("SOLARMIND_APP_DB_PASSWORD must be set to create the application role")
    quoted = password.replace("'", "''")

    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    # The application connects with this role. It owns nothing and has no
    # BYPASSRLS, so Row-Level Security always applies to it.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END $$;
        """
    )
    op.execute(f"ALTER ROLE {APP_ROLE} PASSWORD '{quoted}'")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
