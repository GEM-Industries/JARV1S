"""Automation definition API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps.device_auth import require_owner_id
from core.operations import AutomationDefinitionSummary, list_automation_definitions

router = APIRouter(prefix="/automations", tags=["automations"])


@router.get("/", response_model=list[AutomationDefinitionSummary])
async def list_automations(
    owner_id: str = Depends(require_owner_id),
) -> list[AutomationDefinitionSummary]:
    """List automation definitions for the authenticated owner."""
    return await list_automation_definitions(owner_id)
