"""AI assistant and agent insights."""

import uuid
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from solarmind_api import schemas as sc
from solarmind_api.deps import AuthContext, Perm, TenantDB, get_owned
from solarmind_api.errors import NotFound
from solarmind_api.models import AIConversation, AIMessage, Site
from solarmind_api.permissions import P
from solarmind_api.plans import Feature
from solarmind_api.services import ai, billing

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/ask", response_model=sc.AskOut)
async def ask(
    data: sc.AskIn, request: Request, s: TenantDB, auth: AuthContext = Perm(P.AI_USE)
) -> sc.AskOut:
    """Ask the assistant. It answers only from the caller's authorized data."""
    conversation, message = await ai.ask(
        s, request.app.state.llm_provider, auth, data.question, data.conversation_id
    )
    return sc.AskOut(
        conversation_id=conversation.id, message=sc.AIMessageOut.model_validate(message)
    )


@router.get("/conversations", response_model=list[sc.AIConversationOut])
async def conversations(s: TenantDB, auth: AuthContext = Perm(P.AI_USE)) -> Any:
    rows = await s.scalars(
        select(AIConversation)
        .where(AIConversation.organization_id == auth.org, AIConversation.user_id == auth.user_id)
        .order_by(AIConversation.created_at.desc())
        .limit(50)
    )
    return rows.all()


@router.get("/conversations/{conversation_id}/messages", response_model=list[sc.AIMessageOut])
async def messages(
    conversation_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.AI_USE)
) -> Any:
    owned = await s.scalar(
        select(AIConversation.id).where(
            AIConversation.id == conversation_id,
            AIConversation.organization_id == auth.org,
            AIConversation.user_id == auth.user_id,
        )
    )
    if owned is None:
        raise NotFound("Conversation not found")
    rows = await s.scalars(
        select(AIMessage)
        .where(AIMessage.organization_id == auth.org, AIMessage.conversation_id == conversation_id)
        .order_by(AIMessage.created_at, AIMessage.id)
    )
    return rows.all()


@router.get("/insights/sites/{site_id}")
async def site_insights(
    site_id: uuid.UUID, s: TenantDB, auth: AuthContext = Perm(P.TELEMETRY_READ)
) -> dict[str, Any]:
    """Outputs of the analysis agents for one site (rule-based; no LLM)."""
    site = await get_owned(s, Site, site_id, auth.org)
    entitlements = await billing.get_entitlements(s, auth.org)
    statistical = (
        entitlements.state != "EXPIRED" and Feature.ANOMALY_DETECTION in entitlements.spec.features
    )
    return await ai.site_insights(s, auth.org, site, statistical=statistical)


@router.get("/insights/priorities")
async def priorities(s: TenantDB, auth: AuthContext = Perm(P.ALERTS_READ)) -> list[dict[str, Any]]:
    """Open alerts ordered for intervention (severity, recurrence, age)."""
    return await ai.prioritized_alerts(s, auth.org)
