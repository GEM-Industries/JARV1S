"""
Connection mutations for bespoke OAuth grants (Google, Microsoft, Spotify).

AuthManager owns token custody. This module owns start/complete/disconnect
the same way ``composio.py`` owns Composio connect/disconnect.
"""

from __future__ import annotations

import logging

import httpx

from core.auth.manager import auth_manager
from core.auth.models import OAuthToken
from core.auth.oauth_flow import (
    build_authorize_url,
    complete_oauth_flow,
    consume_flow,
    issue_flow,
)
from core.auth.providers import (
    BUILTIN_PROVIDERS,
    resolve_provider_config,
    scopes_for_provider,
)
from core.integrations.lifecycle._shared import (
    DisconnectResult,
    IntegrationConflictError,
    IntegrationOperationError,
)
from core.integrations.manager import integrations

logger = logging.getLogger(__name__)

_GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"


async def _evict_provider_clients(provider: str) -> None:
    for name in integrations.names_for_provider(provider):
        await integrations.reset(name)


def _authorize_scopes(
    provider: str,
    *,
    plugin: str | None,
    scopes: list[str] | None,
) -> list[str]:
    if scopes:
        registered = list(scopes)
    elif plugin:
        registered = integrations.get_scopes_for_plugin(plugin, provider)
    else:
        registered = integrations.get_scopes_for_provider(provider)
    return scopes_for_provider(provider, registered)


async def start_authorize(
    provider: str,
    *,
    redirect_uri: str,
    plugin: str | None = None,
    scopes: list[str] | None = None,
) -> str:
    """Build a provider consent URL. Raises KeyError if no OAuth app is configured."""
    provider = provider.strip().lower()
    if provider not in BUILTIN_PROVIDERS:
        raise IntegrationConflictError(f"Unknown provider '{provider}'.")

    config, _ = await resolve_provider_config(provider)
    resolved = _authorize_scopes(provider, plugin=plugin, scopes=scopes)
    state, code_challenge = issue_flow(
        provider,
        redirect_uri=redirect_uri,
        scopes=resolved,
    )
    return build_authorize_url(
        config,
        redirect_uri=redirect_uri,
        scopes=resolved,
        state=state,
        code_challenge=code_challenge,
    )


async def complete_grant(state: str, code: str) -> OAuthToken:
    flow = consume_flow(state)
    if not flow:
        raise IntegrationOperationError("State validation failed.")

    try:
        config, _ = await resolve_provider_config(flow.provider)
    except KeyError as exc:
        raise IntegrationOperationError("Provider not configured.") from exc

    try:
        token = await complete_oauth_flow(flow, config, code)
    except Exception as exc:
        logger.error("%s token exchange failed: %s", flow.provider, exc)
        raise IntegrationOperationError(
            "Could not exchange the authorization code for a token."
        ) from exc

    await auth_manager.store_token(token)
    await _evict_provider_clients(flow.provider)
    return token


async def _revoke_google(token: OAuthToken) -> bool:
    hint = token.refresh_token or token.access_token
    if not hint:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _GOOGLE_REVOKE_URI,
                data={"token": hint},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code in {200, 400}:
                return True
            logger.warning("Google revoke returned HTTP %s", resp.status_code)
            return False
    except Exception as exc:
        logger.warning("Google revoke failed: %s", exc)
        return False


async def disconnect_grant(provider: str) -> DisconnectResult:
    provider = provider.strip().lower()
    if provider not in BUILTIN_PROVIDERS:
        raise IntegrationConflictError(f"Unknown provider '{provider}'.")

    remote_disconnected = False
    if provider == "google":
        snapshot = await auth_manager.peek_grant(provider)
        remote_disconnected = await _revoke_google(snapshot) if snapshot else False

    await auth_manager.clear_grant(provider)
    await _evict_provider_clients(provider)
    return DisconnectResult(
        name=provider,
        remote_disconnected=remote_disconnected,
        local_deregistered=False,
    )
