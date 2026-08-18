"""
Gmail client factory and token refresh hook.

Token lifecycle is owned by AuthManager. This module creates the
httpx.AsyncClient for the Gmail API and keeps the Authorization header
current via a thin refresh hook.
"""

import logging
from typing import Any, Dict

import httpx

from core.auth.manager import auth_manager
from core.integrations.manager import NeedsReauth

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def create_gmail_client(config: Dict[str, Any]) -> httpx.AsyncClient:
    """Factory: creates an httpx.AsyncClient pointed at the Gmail API.

    The OAuthToken is pre-validated and injected into config["_oauth_token"]
    by IntegrationManager before this factory runs.
    """
    token = config.get("_oauth_token")
    if not token:
        raise NeedsReauth("gmail")

    return httpx.AsyncClient(
        base_url=GMAIL_API_BASE,
        headers={"Authorization": f"Bearer {token.access_token}"},
        timeout=10.0,
    )


async def refresh_gmail_client(client: httpx.AsyncClient, config: Dict[str, Any]) -> None:
    """Refresh hook: keeps the Authorization header current.

    Delegates to get_token() which checks expiry before refreshing — avoids
    an unconditional HTTP POST to Google on every integrations.get() call.
    """
    token = await auth_manager.get_token("google")
    client.headers["Authorization"] = f"Bearer {token.access_token}"
