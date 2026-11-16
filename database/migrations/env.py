"""Alembic environment.

Migrations run as the database *owner* (SOLARMIND_MIGRATION_DATABASE_URL,
``postgresql+psycopg://``), never as the application role.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from solarmind_api.models import Base

target_metadata = Base.metadata

# Objects managed by TimescaleDB or by hand-written SQL, invisible to autogenerate.
_IGNORED_PREFIXES = ("_hyper_", "telemetry_time_idx")


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    return not (name or "").startswith(_IGNORED_PREFIXES)


def _url() -> str:
    try:
        return os.environ["SOLARMIND_MIGRATION_DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError("SOLARMIND_MIGRATION_DATABASE_URL is not set") from exc


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")
run_migrations_online()
