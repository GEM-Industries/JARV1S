"""REST endpoints for per-device WebSocket auth."""

from __future__ import annotations

import logging
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from api.deps.device_auth import DEVICE_TOKEN_COOKIE, client_host, require_device
from core.auth.device_models import (
    DeviceAuthResult,
    DeviceLocation,
    PairConsumeRequest,
    PairConsumeResponse,
    PairingCodeIssueRequest,
    PairingCodeIssueResponse,
    SatelliteCredentialCreateRequest,
    SatelliteCredentialCreateResponse,
    WsTicketRequest,
    WsTicketResponse,
    device_kind_override_for_client_surface,
)
from core.auth.device_service import (
    DeviceAuthError,
    InvalidDeviceTokenError,
    InvalidPairingCodeError,
    PairingRateLimitError,
    device_auth_service,
)
from core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/device-auth")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _client_key(request: Request) -> str:
    return client_host(request) or "unknown"


def _slugify_node(label: str) -> str:
    slug = _SLUG_RE.sub("-", label.strip().lower()).strip("-")
    return slug or "speaker"


def backend_ws_url_from_request(request: Request) -> str:
    """Build canonical wss/ws URL for satellites from the inbound Host request."""
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    scheme = forwarded_proto or request.url.scheme or "http"
    ws_scheme = "wss" if scheme == "https" else "ws"
    host = host.rstrip("/")
    return f"{ws_scheme}://{host}/api/v1/ws"


@router.post("/pair", response_model=PairConsumeResponse)
async def pair_device(
    request: Request,
    response: Response,
    body: PairConsumeRequest,
) -> PairConsumeResponse:
    """Consume an operator-issued pairing code and return a durable device token once."""
    try:
        result = await device_auth_service.consume_pairing_code(
            body,
            client_key=_client_key(request),
        )
        kind = device_kind_override_for_client_surface(body.client_surface) or "browser"
        try:
            from api.websockets.connection import manager as connection_manager

            live_node_ids = frozenset(
                session.presence.node_id
                for session in connection_manager.list_owner_sessions(result.owner_id)
            )
            await device_auth_service.retire_superseded_credentials(
                owner_id=result.owner_id,
                node_id=result.node_id,
                keep_device_id=result.device_id,
                kind=kind,
                live_node_ids=live_node_ids,
            )
        except Exception:
            # Pairing already succeeded; stale-row cleanup must not consume the
            # one-time code without returning the new credential.
            logger.exception("Could not retire superseded device credentials")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        origin = request.headers.get("origin", "")
        response.set_cookie(
            DEVICE_TOKEN_COOKIE,
            result.device_token,
            httponly=True,
            secure=(
                request.url.scheme == "https"
                or forwarded_proto == "https"
                or origin.startswith("https://")
            ),
            samesite="strict",
            path="/",
            max_age=settings.DEVICE_AUTH_COOKIE_MAX_AGE_S,
        )
        return result
    except PairingRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    except InvalidPairingCodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc


@router.post("/pairing-codes", response_model=PairingCodeIssueResponse)
async def issue_pairing_code(
    body: PairingCodeIssueRequest,
    auth: DeviceAuthResult = Depends(require_device),
) -> PairingCodeIssueResponse:
    """Issue a short-lived pairing code for another browser/phone/satellite."""
    location = DeviceLocation(room_name=body.room_name) if body.room_name else None
    result = await device_auth_service.issue_pairing_code(
        owner_id=auth.owner_id,
        node_label=body.node_label,
        capabilities=body.capabilities,
        location=location,
    )
    return PairingCodeIssueResponse(
        code=result.code,
        expires_at=result.expires_at,
        owner_id=result.owner_id,
        pairing_url=f"?pair={result.code}",
    )


@router.post("/satellites", response_model=SatelliteCredentialCreateResponse)
async def create_satellite_credential(
    request: Request,
    body: SatelliteCredentialCreateRequest,
    auth: DeviceAuthResult = Depends(require_device),
) -> SatelliteCredentialCreateResponse:
    """Mint a durable room-speaker credential and return the token once."""
    label = (body.node_label or "").strip() or "Room Speaker"
    node_id = (body.node_id or "").strip() or f"satellite-{_slugify_node(label)}-{uuid4().hex[:6]}"
    location = None
    if body.ha_area_id or body.room_name:
        room_name = (body.room_name or body.ha_area_id or "").strip() or None
        location = DeviceLocation(
            provider="home_assistant" if body.ha_area_id else "manual",
            room_id=(room_name or "").lower().replace(" ", "_") or None,
            room_name=room_name,
            ha_area_id=body.ha_area_id,
        )
    summary, token = await device_auth_service.create_satellite_credential(
        owner_id=auth.owner_id,
        node_id=node_id,
        node_label=label,
        capabilities=body.capabilities or ["mic", "speaker"],
        location=location,
    )
    return SatelliteCredentialCreateResponse(
        device_id=summary.device_id,
        node_id=summary.node_id,
        node_label=summary.node_label,
        device_token=token,
        backend_ws_url=backend_ws_url_from_request(request),
    )


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def mint_ws_ticket(request: Request, body: WsTicketRequest) -> WsTicketResponse:
    """Exchange a durable device token for a short-lived single-use WebSocket ticket."""
    device_token = body.device_token or request.cookies.get(DEVICE_TOKEN_COOKIE)
    if not device_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token required"
        )
    try:
        return await device_auth_service.mint_ws_ticket(device_token)
    except InvalidDeviceTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except DeviceAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
