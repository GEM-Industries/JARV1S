"""Home Assistant browser-authorization connect flow."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from plugins.smart_home.config import persist_ha_connection
from plugins.smart_home.ha_client import (
    HA_TOKEN_CLIENT_NAME,
    HomeAssistantClient,
    normalize_ha_url,
)

logger = logging.getLogger(__name__)

APP_NAME = "home_assistant"
_FLOW_TTL_SECS = 600
_pending: dict[str, "PendingHaAuthFlow"] = {}


@dataclass(frozen=True, slots=True)
class PendingHaAuthFlow:
    ha_url: str
    client_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class HaAuthorizeStart:
    authorize_url: str
    ha_url: str
    state: str


def _purge_expired() -> None:
    now = time.time()
    expired = [key for key, flow in _pending.items() if flow.expires_at < now]
    for key in expired:
        _pending.pop(key, None)


def issue_ha_auth_flow(
    *,
    ha_url: str,
    origin: str,
) -> HaAuthorizeStart:
    """Create pending HA auth state and return the browser authorize URL."""
    _purge_expired()
    normalized = normalize_ha_url(ha_url)
    cleaned_origin = origin.strip().rstrip("/")
    client_id = f"{cleaned_origin}/"
    redirect_uri = f"{cleaned_origin}/api/v1/smart-home/auth/callback"
    nonce = secrets.token_urlsafe(16)
    expires_at = time.time() + _FLOW_TTL_SECS
    _pending[nonce] = PendingHaAuthFlow(
        ha_url=normalized,
        client_id=client_id,
        expires_at=expires_at,
    )
    state = f"{APP_NAME}:{nonce}"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    authorize_url = f"{normalized}/auth/authorize?{urlencode(params)}"
    return HaAuthorizeStart(authorize_url=authorize_url, ha_url=normalized, state=state)


def consume_ha_auth_flow(state: str) -> PendingHaAuthFlow | None:
    """Validate and consume a pending HA auth state (single use)."""
    _purge_expired()
    parts = state.split(":", 1)
    if len(parts) != 2 or parts[0] != APP_NAME:
        return None
    flow = _pending.pop(parts[1], None)
    if not flow:
        return None
    if time.time() > flow.expires_at:
        return None
    return flow


async def complete_ha_auth_flow(*, code: str, flow: PendingHaAuthFlow) -> str:
    """Exchange code, mint JARV1S long-lived token, persist, revoke temporary refresh."""
    client = HomeAssistantClient(base_url=flow.ha_url)
    try:
        auth = await client.exchange_auth_code(code, client_id=flow.client_id)
        client.token = auth.access_token
        try:
            long_lived = await client.create_long_lived_access_token(HA_TOKEN_CLIENT_NAME)
            await persist_ha_connection(flow.ha_url, long_lived)
            return flow.ha_url
        finally:
            if auth.refresh_token:
                try:
                    await client.revoke_refresh_token(auth.refresh_token)
                except Exception as exc:
                    logger.warning("Could not revoke temporary HA refresh token: %s", exc)
    finally:
        await client.aclose()


def clear_pending_for_tests() -> None:
    _pending.clear()
