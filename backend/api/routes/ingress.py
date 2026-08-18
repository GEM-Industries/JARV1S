"""External ingress configuration and inbound event operations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps.device_auth import require_device
from core.integrations.external_ingress import (
    ExternalIngressState,
    ExternalIngressUpdate,
    configure_external_ingress,
    get_external_ingress_state,
)
from core.integrations.external_webhooks import (
    ExternalCredential,
    ExternalCredentialCreated,
    create_external_credential,
    list_external_credentials,
    revoke_external_credential,
)
from services.inbound_events import (
    InboundEventStats,
    InboundEventSummary,
    inbound_event_service,
)

router = APIRouter(
    prefix="/ingress",
    tags=["ingress"],
    dependencies=[Depends(require_device)],
)


class ExternalCredentialCreateRequest(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=120)


@router.get("/external", response_model=ExternalIngressState)
async def get_ingress() -> ExternalIngressState:
    return await get_external_ingress_state()


@router.post("/external", response_model=ExternalIngressState)
async def set_ingress(update: ExternalIngressUpdate) -> ExternalIngressState:
    try:
        return await configure_external_ingress(update)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/events/stats", response_model=InboundEventStats)
async def get_inbound_stats() -> InboundEventStats:
    return await inbound_event_service.stats()


@router.get("/events/dead-letters", response_model=list[InboundEventSummary])
async def get_dead_letters(limit: int = 50) -> list[InboundEventSummary]:
    return await inbound_event_service.list_dead_letters(limit=min(limit, 200))


@router.get("/events/recent", response_model=list[InboundEventSummary])
async def get_recent_events(limit: int = 50) -> list[InboundEventSummary]:
    return await inbound_event_service.list_recent(limit=min(limit, 200))


@router.post("/events/{event_id}/retry", response_model=InboundEventSummary)
async def retry_inbound_event(event_id: str) -> InboundEventSummary:
    result = await inbound_event_service.retry_event(event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Inbound event not found")
    return result


@router.get("/external-credentials", response_model=list[ExternalCredential])
async def get_external_credentials() -> list[ExternalCredential]:
    return await list_external_credentials()


@router.post("/external-credentials", response_model=ExternalCredentialCreated)
async def post_external_credential(
    request: ExternalCredentialCreateRequest,
) -> ExternalCredentialCreated:
    try:
        return await create_external_credential(source=request.source, label=request.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/external-credentials/{source}")
async def delete_external_credential(source: str) -> dict:
    ok = await revoke_external_credential(source)
    if not ok:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"ok": True}
