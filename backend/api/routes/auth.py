"""
Auth routes for integration OAuth callbacks.

Handles:
  GET  /api/v1/auth/composio/callback          — Composio OAuth redirect endpoint.
  GET  /api/v1/auth/oauth/providers            — Status of each bespoke OAuth provider.
  POST /api/v1/auth/oauth/providers/{p}/configure — Save client_id/secret for a provider.
  POST /api/v1/auth/oauth/providers/{p}/authorize — Begin OAuth (browser consent + PKCE).
  GET  /api/v1/auth/oauth/callback             — OAuth redirect receiver.
"""

import logging
import webbrowser
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.deps.device_auth import require_device
from api.oauth_support import (
    assert_allowed_oauth_origin,
    publish_oauth_changed,
    render_oauth_callback_page,
)
from core.auth.oauth_flow import consume_callback_nonce, oauth_redirect_uri
from core.auth.providers import (
    BUILTIN_PROVIDERS,
    PROVIDER_URIS,
    resolve_provider_config,
)
from core.integrations.lifecycle import (
    IntegrationConflictError,
    IntegrationOperationError,
    complete_grant,
    disconnect_grant,
    start_authorize,
)

router = APIRouter(prefix="/auth")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class ConfigureRequest(BaseModel):
    client_id: str
    client_secret: Optional[str] = None  # None for Microsoft public client


class AuthorizeRequest(BaseModel):
    origin: str  # e.g. "http://localhost:5173" — used to build redirect_uri
    plugin: Optional[str] = None
    scopes: Optional[list[str]] = None


class OpenExternalRequest(BaseModel):
    url: str


_OAUTH_HOST_SUFFIXES = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "accounts.spotify.com",
    "composio.dev",
    "composio.io",
)


def _is_allowed_oauth_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _OAUTH_HOST_SUFFIXES
    )


class ProviderStatus(BaseModel):
    provider: str
    connectable: bool
    connected: bool
    account_email: Optional[str] = None
    config_mode: Optional[Literal["product", "self_managed"]] = None


# ---------------------------------------------------------------------------
# Bespoke OAuth endpoints
# ---------------------------------------------------------------------------


@router.get("/oauth/providers")
async def get_oauth_providers(_auth=Depends(require_device)) -> list[ProviderStatus]:
    """Return connectable/connected status for each bespoke OAuth provider."""
    from core.auth.manager import auth_manager

    results = []
    for provider in PROVIDER_URIS:
        token = await auth_manager.peek_grant(provider)
        try:
            _, mode = await resolve_provider_config(provider)
            connectable = True
            config_mode = mode
        except KeyError:
            connectable = False
            config_mode = None
        results.append(
            ProviderStatus(
                provider=provider,
                connectable=connectable,
                connected=token is not None,
                account_email=token.account_email if token else None,
                config_mode=config_mode,
            )
        )

    return results


@router.delete("/oauth/providers/{provider}")
async def delete_provider(provider: str, _auth=Depends(require_device)) -> dict:
    """Disconnect the user grant. Leaves the OAuth app registration in place."""
    try:
        await disconnect_grant(provider)
    except IntegrationConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted", "provider": provider}


@router.post("/oauth/providers/{provider}/configure")
async def configure_provider(
    provider: str,
    body: ConfigureRequest,
    _auth=Depends(require_device),
) -> dict:
    """Store client_id/secret for a self-managed OAuth provider (advanced path)."""
    if provider not in BUILTIN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{provider}'. Valid: {', '.join(PROVIDER_URIS)}",
        )

    from core.auth.manager import auth_manager
    from core.auth.models import ProviderConfig

    uris = PROVIDER_URIS[provider]
    config = ProviderConfig(
        provider=provider,
        client_id=body.client_id,
        client_secret=body.client_secret,
        token_uri=uris["token_uri"],
        auth_uri=uris["auth_uri"],
    )
    await auth_manager.store_provider_config(config)
    logger.info("Stored self-managed provider config for '%s'", provider)
    return {"success": True}


@router.post("/oauth/providers/{provider}/authorize")
async def authorize_provider(
    provider: str,
    body: AuthorizeRequest,
    request: Request,
    _auth=Depends(require_device),
) -> dict:
    """
    Begin browser-consent OAuth for a provider.

    Returns { authorize_url } — frontend opens this in a popup.
    """
    origin = assert_allowed_oauth_origin(body.origin, request.headers.get("origin"))
    redirect_uri = oauth_redirect_uri(origin, provider)
    try:
        authorize_url = await start_authorize(
            provider,
            redirect_uri=redirect_uri,
            plugin=body.plugin,
            scopes=body.scopes,
        )
    except IntegrationConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail=f"No OAuth app configured for '{provider}'. Use Advanced to add your own.",
        )
    return {"authorize_url": authorize_url}


@router.post("/oauth/open-external")
async def open_external_oauth(
    body: OpenExternalRequest,
    _auth=Depends(require_device),
) -> dict[str, bool]:
    """
    Open an OAuth authorization URL in the user's default system browser.

    Used by the desktop host app where embedded WebViews block window.open popups.
    """
    if not _is_allowed_oauth_url(body.url):
        raise HTTPException(
            status_code=400,
            detail="URL is not an allowed OAuth authorization endpoint.",
        )
    if not webbrowser.open(body.url, new=2):
        raise HTTPException(
            status_code=500, detail="Could not open the system browser."
        )
    return {"opened": True}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = Query(None, alias="error_description"),
) -> HTMLResponse:
    """
    OAuth redirect callback for Google, Microsoft, and Spotify.

    Exchanges the authorization code for tokens, resolves the account email,
    stores via AuthManager, and renders the postMessage page to close the popup.
    """
    provider_hint = "google"
    if state and ":" in state:
        provider_hint = state.split(":", 1)[0]

    if error:
        logger.warning(
            "OAuth callback error (%s): %s — %s",
            provider_hint,
            error,
            error_description,
        )
        await publish_oauth_changed(app=provider_hint, success=False)
        return HTMLResponse(
            render_oauth_callback_page(
                title="Authorization Failed",
                message=f"Authorization failed: {error_description or error}",
                success=False,
                app_name=provider_hint,
            )
        )

    if not code or not state:
        await publish_oauth_changed(app=provider_hint, success=False)
        return HTMLResponse(
            render_oauth_callback_page(
                title="Authorization Incomplete",
                message="The authorization was not completed. You can close this window.",
                success=False,
                app_name=provider_hint,
            )
        )

    try:
        token = await complete_grant(state, code)
    except IntegrationOperationError as e:
        await publish_oauth_changed(app=provider_hint, success=False)
        return HTMLResponse(
            render_oauth_callback_page(
                title="Authorization Failed",
                message=str(e),
                success=False,
                app_name=provider_hint,
            )
        )

    await publish_oauth_changed(app=token.provider, success=True, loaded=True)

    display = token.provider.title()
    logger.info(
        "%s OAuth complete for %s (%d scopes)",
        display,
        token.account_email,
        len(token.granted_scopes),
    )
    return HTMLResponse(
        render_oauth_callback_page(
            title=f"{display} Connected",
            message=f"{display} is connected as {token.account_email}. You can close this window.",
            success=True,
            app_name=token.provider,
            loaded=True,
        )
    )


# ---------------------------------------------------------------------------
# Composio OAuth callback (unchanged)
# ---------------------------------------------------------------------------


@router.get("/composio/callback", response_class=HTMLResponse)
async def composio_callback(
    request: Request,
    status: Optional[str] = None,
    error: Optional[str] = None,
    state: Optional[str] = None,
) -> HTMLResponse:
    """
    Composio OAuth redirect callback.

    Composio redirects here after the user completes (or cancels) the
    OAuth flow. Query params (v3): status, connected_account_id, app_name, error.

    On success: hot-loads the app's tools into the live registry and
    ToolRouter, then returns a confirmation page the user can close.
    """
    expected_app = consume_callback_nonce(state or "")
    if not expected_app:
        return HTMLResponse(
            render_oauth_callback_page(
                title="Authorization Error",
                message="State validation failed. Please start the connection again.",
                success=False,
            ),
            status_code=400,
        )

    params = request.query_params
    connected_account_id = params.get("connected_account_id")
    app_name = params.get("app_name")

    if error or status == "error":
        reason = error or "Unknown error"
        logger.warning(
            "Composio OAuth callback error for app '%s': %s", app_name, reason
        )
        if app_name:
            await publish_oauth_changed(app=app_name, success=False, kind="composio")
        return HTMLResponse(
            render_oauth_callback_page(
                title="Connection Failed",
                message=f"Could not connect {app_name or 'the integration'}: {reason}",
                success=False,
                app_name=app_name or "",
            )
        )

    if status != "success" or not connected_account_id:
        logger.info(
            "Composio OAuth callback: incomplete params (status=%s, account_id=%s, raw_qs=%s)",
            status,
            connected_account_id,
            str(params),
        )
        if app_name:
            await publish_oauth_changed(app=app_name, success=False, kind="composio")
        return HTMLResponse(
            render_oauth_callback_page(
                title="Connection Incomplete",
                message="The authorization was not completed. You can close this window.",
                success=False,
                app_name=app_name or "",
            )
        )

    resolved_app = app_name or await _resolve_app_name(connected_account_id)
    if not resolved_app:
        logger.warning(
            "Composio callback: could not resolve app name for account %s",
            connected_account_id,
        )
        if app_name:
            await publish_oauth_changed(app=app_name, success=False, kind="composio")
        return HTMLResponse(
            render_oauth_callback_page(
                title="Connection Error",
                message="Authorization succeeded but the app could not be identified. "
                "Restart JARV1S to load the new integration.",
                success=False,
                app_name="",
            )
        )
    if resolved_app.casefold() != expected_app.casefold():
        logger.warning(
            "Composio callback app mismatch: expected=%s resolved=%s",
            expected_app,
            resolved_app,
        )
        return HTMLResponse(
            render_oauth_callback_page(
                title="Connection Error",
                message="The authorization result did not match the requested integration.",
                success=False,
            ),
            status_code=400,
        )

    from core.integrations.lifecycle import (
        IntegrationLifecycleError,
        reconcile_integration,
    )

    try:
        result = await reconcile_integration(resolved_app)
        loaded = result.loaded
    except IntegrationLifecycleError as e:
        logger.error(
            "Failed to reconcile Composio app '%s': %s", resolved_app, e, exc_info=True
        )
        loaded = False

    await publish_oauth_changed(
        app=resolved_app,
        success=True,
        loaded=loaded,
        kind="composio",
    )

    if loaded:
        logger.info("Composio app '%s' tools loaded via callback", resolved_app)
        return HTMLResponse(
            render_oauth_callback_page(
                title=f"{resolved_app.title()} Connected",
                message=f"{resolved_app.title()} is now connected. "
                "You can ask JARV1S to use it right away — close this window to continue.",
                success=True,
                app_name=resolved_app,
                loaded=True,
            )
        )
    else:
        return HTMLResponse(
            render_oauth_callback_page(
                title=f"{resolved_app.title()} Connected",
                message=f"{resolved_app.title()} was authorized. "
                "Restart JARV1S to load the integration if tools are not yet available.",
                success=True,
                app_name=resolved_app,
                loaded=False,
            )
        )


async def _resolve_app_name(connected_account_id: str) -> Optional[str]:
    """Query Composio to get the app name for a connected account ID."""
    from core.integrations.composio_gateway import get_composio_gateway

    gateway = get_composio_gateway()
    if not gateway:
        return None

    try:
        return await gateway.resolve_app_name(connected_account_id)
    except Exception as e:
        logger.warning(
            "Could not resolve app name for account %s: %s", connected_account_id, e
        )
        return None
