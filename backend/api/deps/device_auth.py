"""Device-token authentication for REST API routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth.device_models import DeviceAuthResult, DeviceLocation
from core.auth.device_service import InvalidDeviceTokenError, device_auth_service
from core.config import settings

_bearer = HTTPBearer(auto_error=False)
DEVICE_TOKEN_COOKIE = "jarvis_device_token"


def client_host(request: Request) -> str | None:
    """Resolve the client without trusting forwarding headers from remote peers."""
    peer = request.client.host if request.client else None
    if not device_auth_service.is_loopback_host(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return peer


def extract_device_token(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
) -> str | None:
    if creds and creds.scheme.lower() == "bearer" and creds.credentials:
        return creds.credentials.strip()
    raw = request.headers.get("x-device-token")
    if raw and raw.strip():
        return raw.strip()
    cookie = request.cookies.get(DEVICE_TOKEN_COOKIE)
    if cookie and cookie.strip():
        return cookie.strip()
    return None


def _local_bypass_identity() -> DeviceAuthResult:
    kind = device_auth_service.local_bypass_device_kind()
    packaged_host = kind == "desktop"
    return DeviceAuthResult(
        device_id="local-host" if packaged_host else "dev-bypass",
        owner_id=settings.DEFAULT_USER_ID,
        node_id="host" if packaged_host else "default",
        node_label="Jarvis Host" if packaged_host else "Local Development",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind=kind,
    )


async def require_device(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> DeviceAuthResult:
    """Require a durable device token, or localhost development bypass."""
    if not settings.DEVICE_AUTH_REQUIRED or device_auth_service.local_bypass_allowed(
        host=client_host(request)
    ):
        return _local_bypass_identity()

    token = extract_device_token(request, creds)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device auth required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await device_auth_service.authenticate_device_token(token)
    except InvalidDeviceTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def require_owner_id(auth: DeviceAuthResult = Depends(require_device)) -> str:
    return auth.owner_id
