"""Database access with tenant context.

Every transaction declares who it runs for through two transaction-local
settings read by the Row-Level Security policies:

* ``app.current_org_id``: the organization whose rows are visible;
* ``app.rls_bypass``: ``on`` only for narrowly scoped system work (login,
  registration, gateway key lookup, background jobs, platform administration).

``set_config(..., true)`` is transaction-local, so a pooled connection can
never carry one tenant's context into another request.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_SET_CONTEXT = text(
    "SELECT set_config('app.current_org_id', :org, true), set_config('app.rls_bypass', :bypass, true)"
)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def tenant(self, organization_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """One transaction restricted to a single organization."""
        async with self._sessions() as session, session.begin():
            await session.execute(_SET_CONTEXT, {"org": str(organization_id), "bypass": "off"})
            yield session

    @asynccontextmanager
    async def system(self) -> AsyncIterator[AsyncSession]:
        """One transaction that bypasses tenant filtering. Use sparingly."""
        async with self._sessions() as session, session.begin():
            await session.execute(_SET_CONTEXT, {"org": "", "bypass": "on"})
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
