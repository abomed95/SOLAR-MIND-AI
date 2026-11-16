"""Audit trail. Rows are append-only for the application role."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from solarmind_api.models import AuditLog


def audit(
    session: AsyncSession,
    *,
    action: str,
    organization_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(
        AuditLog(
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            ip=ip,
        )
    )
