"""PKCE helpers and short-lived pending OAuth flow state."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

from core.auth.models import OAuthToken, ProviderConfig
from core.auth.providers import PROVIDER_URIS

logger = logging.getLogger(__name__)

_FLOW_TTL_SECS = 600

_pending: dict[str, PendingOAuthFlow] = {}
_pending_callbacks: dict[str, tuple[str, float]] = {}


@dataclass(frozen=True, slots=True)
class PendingOAuthFlow:
    provider: str
    code_verifier: str
    redirect_uri: str
    scopes: list[str]
    expires_at: float


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = secrets.token_urlsafe(64).rstrip("=")[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _purge_expired() -> None:
    now = datetime.now(timezone.utc).timestamp()
    expired = [k for k, flow in _pending.items() if flow.expires_at < now]
    for key in expired:
        _pending.pop(key, None)
    expired_callbacks = [
        key for key, (_, expires_at) in _pending_callbacks.items() if expires_at < now
    ]
    for key in expired_callbacks:
        _pending_callbacks.pop(key, None)


def issue_callback_nonce(subject: str) -> str:
    """Create short-lived state for a hosted OAuth callback."""
    _purge_expired()
    nonce = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc).timestamp() + _FLOW_TTL_SECS
    _pending_callbacks[nonce] = (subject, expires_at)
    return nonce


def consume_callback_nonce(nonce: str) -> str | None:
    """Consume hosted callback state once and return its expected subject."""
    _purge_expired()
    pending = _pending_callbacks.pop(nonce, None)
    if not pending:
        return None
    subject, expires_at = pending
    if datetime.now(timezone.utc).timestamp() > expires_at:
        return None
    return subject


def issue_flow(
    provider: str,
    *,
    redirect_uri: str,
    scopes: list[str],
) -> tuple[str, str]:
    """Create pending flow; return (state, code_challenge) for authorize URL."""
    _purge_expired()
    nonce = secrets.token_urlsafe(16)
    verifier, challenge = generate_pkce_pair()
    expires_at = datetime.now(timezone.utc).timestamp() + _FLOW_TTL_SECS
    _pending[nonce] = PendingOAuthFlow(
        provider=provider,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        scopes=scopes,
        expires_at=expires_at,
    )
    state = f"{provider}:{nonce}"
    return state, challenge


def consume_flow(state: str) -> Optional[PendingOAuthFlow]:
    """Validate state and return the pending flow (single use)."""
    _purge_expired()
    parts = state.split(":", 1)
    if len(parts) != 2:
        return None
    provider_from_state, nonce = parts
    flow = _pending.pop(nonce, None)
    if not flow or flow.provider != provider_from_state:
        return None
    if datetime.now(timezone.utc).timestamp() > flow.expires_at:
        return None
    return flow


def oauth_redirect_uri(origin: str, provider: str) -> str:
    """Build the hosted callback URI. Spotify forbids `localhost`; use loopback IPv4."""
    origin = origin.strip().rstrip("/")
    if provider == "spotify":
        parsed = urlparse(origin)
        if (parsed.hostname or "").lower() == "localhost":
            netloc = parsed.netloc.replace("localhost", "127.0.0.1", 1)
            origin = urlunparse(parsed._replace(netloc=netloc)).rstrip("/")
    return f"{origin}/api/v1/auth/oauth/callback"


def build_authorize_url(
    config: ProviderConfig,
    *,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if config.provider == "google":
        params.update(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
    return f"{config.auth_uri}?{urlencode(params)}"


def token_exchange_payload(
    config: ProviderConfig,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, str]:
    payload: dict[str, str] = {
        "client_id": config.client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if config.client_secret:
        payload["client_secret"] = config.client_secret
    return payload


async def exchange_code(
    config: ProviderConfig,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    payload = token_exchange_payload(
        config,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(config.token_uri, data=payload)
        resp.raise_for_status()
        return resp.json()


async def resolve_account_email(provider: str, access_token: str) -> str:
    uris = PROVIDER_URIS.get(provider, {})
    userinfo_uri = uris.get("userinfo_uri")
    if not userinfo_uri:
        return f"unknown@{provider}.local"

    defaults = {
        "google": "unknown@gmail.com",
        "microsoft": "unknown@outlook.com",
        "spotify": "unknown@spotify.local",
    }
    fallback = defaults.get(provider, f"unknown@{provider}.local")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                userinfo_uri,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                return fallback
            data = resp.json()
            if provider == "google":
                return data.get("email", fallback)
            if provider == "spotify":
                return data.get("email") or data.get("id") or fallback
            return data.get("mail") or data.get("userPrincipalName", fallback)
    except Exception:
        return fallback


async def complete_oauth_flow(
    flow: PendingOAuthFlow,
    config: ProviderConfig,
    code: str,
) -> OAuthToken:
    token_data = await exchange_code(
        config,
        code=code,
        redirect_uri=flow.redirect_uri,
        code_verifier=flow.code_verifier,
    )
    account_email = await resolve_account_email(
        flow.provider, token_data["access_token"]
    )
    now = datetime.now(timezone.utc)
    expires_in = token_data.get("expires_in", 3600)
    granted = (
        token_data.get("scope", "").split() if token_data.get("scope") else flow.scopes
    )

    return OAuthToken(
        provider=flow.provider,
        account_email=account_email,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token", ""),
        token_expiry=now + timedelta(seconds=expires_in),
        granted_scopes=granted,
    )
