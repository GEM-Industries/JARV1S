"""
Spotify Web API client factory and request helper.

Token lifecycle is owned by AuthManager. This module creates the
httpx.AsyncClient and maps expected provider errors (reauth, allowlist,
Premium, no device). Unexpected HTTP failures raise.
"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from core.auth.manager import auth_manager
from core.integrations.manager import NeedsReauth

SPOTIFY_API_BASE = "https://api.spotify.com/v1"

SPOTIFY_SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-modify",
]

NO_DEVICE_MESSAGE = "No Spotify devices available — open Spotify on a device first"
ALLOWLIST_MESSAGE = (
    "This Spotify user is not on the app's allowlist. "
    "Add them in the Spotify Developer Dashboard under User Management."
)


class SpotifyClientError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def create_spotify_client(config: Dict[str, Any]) -> httpx.AsyncClient:
    token = config.get("_oauth_token")
    if not token:
        raise NeedsReauth("spotify")

    return httpx.AsyncClient(
        base_url=SPOTIFY_API_BASE,
        headers={"Authorization": f"Bearer {token.access_token}"},
        timeout=10.0,
    )


async def refresh_spotify_client(client: httpx.AsyncClient, config: Dict[str, Any]) -> None:
    token = await auth_manager.get_token("spotify")
    client.headers["Authorization"] = f"Bearer {token.access_token}"


def _error_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text or ""


def _error_detail(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("reason") or "")
        if isinstance(err, str):
            return err
    return str(body or "")


def _expected_client_message(status: int, detail: str, path: str) -> str | None:
    lowered = detail.lower()
    if status == 403:
        if "premium" in lowered:
            return "Spotify Premium is required for playback control."
        return ALLOWLIST_MESSAGE
    if status == 404 and path.startswith("/me/player"):
        return NO_DEVICE_MESSAGE
    return None


async def api_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
) -> Any:
    resp = await client.request(method, path, params=params, json=json)
    if resp.status_code == 401:
        raise NeedsReauth("spotify")
    if resp.status_code >= 400:
        mapped = _expected_client_message(
            resp.status_code, _error_detail(_error_body(resp)), path
        )
        if mapped:
            raise SpotifyClientError(mapped)
        resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()
