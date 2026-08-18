"""Direct Home Assistant REST + WebSocket client."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect

WS_TIMEOUT_S = 30.0
REST_TIMEOUT_S = 15.0
# Home Assistant allows one long-lived token per client_name. Disconnect must revoke it.
HA_TOKEN_CLIENT_NAME = "JARV1S"


class HomeAssistantError(Exception):
    """Base error for HA client operations."""


class HomeAssistantAuthError(HomeAssistantError):
    pass


class HomeAssistantConnectionError(HomeAssistantError):
    pass


class HomeAssistantOnboardingError(HomeAssistantError):
    pass


@dataclass(frozen=True, slots=True)
class OnboardingUserResult:
    auth_code: str


@dataclass(frozen=True, slots=True)
class AuthTokenResult:
    access_token: str
    refresh_token: str | None = None


def normalize_ha_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("HA_URL is required")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid HA_URL: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError("HA_URL must not include credentials")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def ha_ws_url(base_url: str) -> str:
    parsed = urlparse(normalize_ha_url(base_url))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/api/websocket", "", "", ""))


class HomeAssistantClient:
    """Async client for Home Assistant REST and WebSocket APIs."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = normalize_ha_url(base_url)
        self.token = (token or "").strip() or None
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=REST_TIMEOUT_S)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise HomeAssistantAuthError("HA_TOKEN is not configured")
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def ping(self) -> dict[str, Any]:
        """GET /api/ — verifies reachability and token when configured."""
        url = urljoin(self.base_url + "/", "api/")
        headers = self._headers() if self.token else {}
        try:
            resp = await self._http.get(url, headers=headers)
        except httpx.RequestError as e:
            raise HomeAssistantConnectionError(f"Cannot reach Home Assistant at {self.base_url}") from e
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        if resp.status_code >= 400:
            raise HomeAssistantConnectionError(f"Home Assistant returned HTTP {resp.status_code}")
        return resp.json()

    async def get_config(self) -> dict[str, Any]:
        """GET /api/config — home metadata incl. latitude/longitude/location_name/time_zone."""
        url = urljoin(self.base_url + "/", "api/config")
        resp = await self._http.get(url, headers=self._headers())
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def get_states(self) -> list[dict[str, Any]]:
        url = urljoin(self.base_url + "/", "api/states")
        resp = await self._http.get(url, headers=self._headers())
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", f"api/states/{entity_id}")
        resp = await self._http.get(url, headers=self._headers())
        if resp.status_code == 404:
            raise HomeAssistantError(f"Entity not found: {entity_id}")
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        return resp.json()

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        entity_id: str | list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = urljoin(self.base_url + "/", f"api/services/{domain}/{service}")
        payload: dict[str, Any] = dict(data or {})
        if entity_id:
            payload.setdefault("entity_id", entity_id)
        resp = await self._http.post(url, headers=self._headers(), json=payload)
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        if resp.status_code >= 400:
            detail = resp.text.strip()
            suffix = f": {detail}" if detail else ""
            raise HomeAssistantError(f"Home Assistant service {domain}.{service} failed with HTTP {resp.status_code}{suffix}")
        if not resp.content:
            return None
        return resp.json()

    async def ws_command(self, command_type: str, **kwargs: Any) -> Any:
        """Send one authenticated WebSocket command and return the result."""
        if not self.token:
            raise HomeAssistantAuthError("HA_TOKEN is required for WebSocket commands")

        ws_url = ha_ws_url(self.base_url)
        try:
            async with ws_connect(ws_url, open_timeout=REST_TIMEOUT_S) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=WS_TIMEOUT_S)
                msg = json.loads(raw)
                if msg.get("type") != "auth_required":
                    raise HomeAssistantConnectionError("Unexpected WebSocket greeting")

                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                raw = await asyncio.wait_for(ws.recv(), timeout=WS_TIMEOUT_S)
                auth_msg = json.loads(raw)
                if auth_msg.get("type") != "auth_ok":
                    raise HomeAssistantAuthError("WebSocket authentication failed")

                req_id = 1
                await ws.send(json.dumps({"id": req_id, "type": command_type, **kwargs}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WS_TIMEOUT_S)
                    resp = json.loads(raw)
                    if resp.get("id") != req_id:
                        continue
                    if not resp.get("success", False):
                        err = resp.get("error") or {}
                        raise HomeAssistantError(err.get("message") or f"WebSocket command failed: {command_type}")
                    return resp.get("result")
        except asyncio.TimeoutError as e:
            raise HomeAssistantConnectionError("Home Assistant WebSocket timed out") from e
        except websockets.exceptions.WebSocketException as e:
            raise HomeAssistantConnectionError(f"WebSocket error: {e}") from e

    async def list_areas(self) -> list[dict[str, Any]]:
        result = await self.ws_command("config/area_registry/list")
        return result if isinstance(result, list) else []

    async def list_devices(self) -> list[dict[str, Any]]:
        result = await self.ws_command("config/device_registry/list")
        return result if isinstance(result, list) else []

    async def list_entities_registry(self) -> list[dict[str, Any]]:
        result = await self.ws_command("config/entity_registry/list")
        return result if isinstance(result, list) else []

    async def list_config_entries(self, domain: str | None = None) -> list[dict[str, Any]]:
        if domain:
            result = await self.ws_command("config_entries/get", domain=domain)
        else:
            result = await self.ws_command("config_entries/get")
        return result if isinstance(result, list) else []

    async def reload_config_entry(self, entry_id: str) -> None:
        """Reload one HA config entry via the homeassistant.reload_config_entry service."""
        await self.call_service(
            "homeassistant",
            "reload_config_entry",
            data={"entry_id": entry_id},
        )

    async def create_area(self, name: str) -> dict[str, Any]:
        result = await self.ws_command("config/area_registry/create", name=name)
        return result if isinstance(result, dict) else {}

    async def update_area(self, area_id: str, *, name: str) -> dict[str, Any]:
        result = await self.ws_command("config/area_registry/update", area_id=area_id, name=name)
        return result if isinstance(result, dict) else {}

    async def delete_area(self, area_id: str) -> None:
        await self.ws_command("config/area_registry/delete", area_id=area_id)

    async def update_device(
        self,
        device_id: str,
        *,
        area_id: str | None = None,
        name_by_user: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"device_id": device_id}
        if area_id is not None:
            payload["area_id"] = area_id
        if name_by_user is not None:
            payload["name_by_user"] = name_by_user
        result = await self.ws_command("config/device_registry/update", **payload)
        return result if isinstance(result, dict) else {}

    async def update_entity(
        self,
        entity_id: str,
        *,
        name: str | None = None,
        area_id: str | None = None,
        new_entity_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"entity_id": entity_id}
        if name is not None:
            payload["name"] = name
        if area_id is not None:
            payload["area_id"] = area_id
        if new_entity_id is not None:
            payload["new_entity_id"] = new_entity_id
        result = await self.ws_command("config/entity_registry/update", **payload)
        return result if isinstance(result, dict) else {}

    async def get_config_flow_progress(self) -> list[dict[str, Any]]:
        """In-progress config flows — used by the Tapo/Kasa second vertical slice."""
        result = await self.ws_command("config_entries/flow/progress")
        return result if isinstance(result, list) else []

    async def create_config_flow(self, handler: str) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", "api/config/config_entries/flow")
        resp = await self._http.post(url, headers=self._headers(), json={"handler": handler})
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def fetch_config_flow(self, flow_id: str) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", f"api/config/config_entries/flow/{flow_id}")
        resp = await self._http.get(url, headers=self._headers())
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def handle_config_flow_step(self, flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", f"api/config/config_entries/flow/{flow_id}")
        resp = await self._http.post(url, headers=self._headers(), json=data)
        if resp.status_code == 401:
            raise HomeAssistantAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        result = resp.json()
        return result if isinstance(result, dict) else {}

    async def current_user(self) -> dict[str, Any]:
        result = await self.ws_command("auth/current_user")
        return result if isinstance(result, dict) else {}

    async def list_refresh_tokens(self) -> list[dict[str, Any]]:
        result = await self.ws_command("auth/refresh_tokens")
        return result if isinstance(result, list) else []

    async def delete_refresh_token(self, refresh_token_id: str) -> None:
        await self.ws_command("auth/delete_refresh_token", refresh_token_id=refresh_token_id)

    async def delete_long_lived_access_tokens(self, client_name: str) -> int:
        """Delete long-lived tokens with this client_name. Returns how many were removed."""
        deleted = 0
        for token in await self.list_refresh_tokens():
            if (
                token.get("client_name") == client_name
                and token.get("type") == "long_lived_access_token"
                and isinstance(token.get("id"), str)
            ):
                await self.delete_refresh_token(token["id"])
                deleted += 1
        return deleted

    async def create_long_lived_access_token(self, client_name: str, *, lifespan_days: int = 3650) -> str:
        """Create a long-lived token. HA allows one per client_name, so replace any existing."""
        await self.delete_long_lived_access_tokens(client_name)
        token = await self.ws_command(
            "auth/long_lived_access_token",
            client_name=client_name,
            lifespan=lifespan_days,
        )
        if not isinstance(token, str) or not token:
            raise HomeAssistantError("Failed to create long-lived access token")
        return token

    async def onboarding_pending(self) -> bool:
        """True when HA first-run onboarding is still incomplete."""
        url = urljoin(self.base_url + "/", "api/onboarding")
        try:
            resp = await self._http.get(url)
        except httpx.RequestError:
            return False
        if resp.status_code != 200:
            return False
        data = resp.json()
        if not isinstance(data, list):
            return False
        return any(step.get("done") is False for step in data)

    def indieauth_client_id(self) -> str:
        """IndieAuth client_id — must match the URL used during onboarding token exchange."""
        return f"{self.base_url}/"

    async def get_onboarding_steps(self) -> list[dict[str, Any]]:
        url = urljoin(self.base_url + "/", "api/onboarding")
        resp = await self._http.get(url)
        if resp.status_code != 200:
            raise HomeAssistantOnboardingError(f"Could not read onboarding status: HTTP {resp.status_code}")
        data = resp.json()
        return data if isinstance(data, list) else []

    async def _post_onboarding_step(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if authenticated:
            headers.update(self._headers())
        resp = await self._http.post(url, headers=headers, json=json_body or {})
        if resp.status_code == 403:
            raise HomeAssistantOnboardingError(f"Onboarding step already complete: {path}")
        if resp.status_code >= 400:
            raise HomeAssistantOnboardingError(f"Onboarding step failed ({path}): HTTP {resp.status_code}")
        if not resp.content:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def complete_core_config(self) -> None:
        await self._post_onboarding_step("api/onboarding/core_config", authenticated=True)

    async def complete_analytics(self, *, analytics_opt_in: bool = False) -> None:
        # HA 2025.4 marks analytics done from an empty POST; keep the parameter for CLI intent.
        await self._post_onboarding_step("api/onboarding/analytics", authenticated=True)

    async def complete_integration(self, *, client_id: str | None = None, redirect_uri: str | None = None) -> None:
        cid = client_id or self.indieauth_client_id()
        redirect = redirect_uri or f"{cid}?auth_callback=1"
        await self._post_onboarding_step(
            "api/onboarding/integration",
            json_body={"client_id": cid, "redirect_uri": redirect},
            authenticated=True,
        )

    async def complete_bootstrap_onboarding(
        self,
        *,
        owner_name: str,
        username: str,
        password: str,
        language: str = "en",
        analytics_opt_in: bool = False,
    ) -> str:
        """
        Run full HA first-run onboarding and return a JARV1S long-lived token.

        Order: user → auth code exchange → short-lived token → core_config →
        analytics → integration → WS long_lived_access_token.
        """
        if not await self.onboarding_pending():
            raise HomeAssistantOnboardingError("Home Assistant onboarding is already complete")

        client_id = self.indieauth_client_id()
        user_result = await self.create_onboarding_user(
            name=owner_name,
            username=username,
            password=password,
            language=language,
        )
        auth = await self.exchange_auth_code(user_result.auth_code, client_id=client_id)
        self.token = auth.access_token

        await self.complete_core_config()
        await self.complete_analytics(analytics_opt_in=analytics_opt_in)
        await self.complete_integration(client_id=client_id)

        if await self.onboarding_pending():
            steps = await self.get_onboarding_steps()
            pending = [s["step"] for s in steps if not s.get("done")]
            raise HomeAssistantOnboardingError(
                f"Onboarding incomplete after bootstrap steps. Pending: {', '.join(pending)}"
            )

        return await self.create_long_lived_access_token(HA_TOKEN_CLIENT_NAME)

    async def create_onboarding_user(
        self,
        *,
        name: str,
        username: str,
        password: str,
        language: str = "en",
    ) -> OnboardingUserResult:
        client_id = f"{self.base_url}/"
        url = urljoin(self.base_url + "/", "api/onboarding/users")
        payload = {
            "client_id": client_id,
            "name": name,
            "username": username,
            "password": password,
            "language": language,
        }
        try:
            resp = await self._http.post(url, json=payload)
        except httpx.RequestError as e:
            raise HomeAssistantConnectionError(f"Cannot reach Home Assistant at {self.base_url}") from e
        if resp.status_code == 403:
            raise HomeAssistantOnboardingError("Home Assistant onboarding user step is already complete")
        if resp.status_code >= 400:
            raise HomeAssistantOnboardingError(f"Onboarding failed: HTTP {resp.status_code}")
        data = resp.json()
        auth_code = data.get("auth_code")
        if not auth_code:
            raise HomeAssistantOnboardingError("Onboarding did not return an auth_code")
        return OnboardingUserResult(auth_code=auth_code)

    async def exchange_auth_code(self, auth_code: str, *, client_id: str | None = None) -> AuthTokenResult:
        url = urljoin(self.base_url + "/", "auth/token")
        cid = client_id or f"{self.base_url}/"
        payload = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": cid,
        }
        resp = await self._http.post(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise HomeAssistantAuthError(f"Auth code exchange failed: HTTP {resp.status_code}")
        data = resp.json()
        access = data.get("access_token")
        if not access:
            raise HomeAssistantAuthError("Auth code exchange did not return an access_token")
        return AuthTokenResult(
            access_token=access,
            refresh_token=data.get("refresh_token"),
        )

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        """Revoke a temporary OAuth refresh token after minting a long-lived token."""
        url = urljoin(self.base_url + "/", "auth/revoke")
        resp = await self._http.post(
            url,
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise HomeAssistantAuthError(f"Token revoke failed: HTTP {resp.status_code}")


async def create_ha_client(config: dict[str, Any]) -> HomeAssistantClient:
    """IntegrationManager factory for smart_home."""
    url = config.get("HA_URL")
    token = config.get("HA_TOKEN")
    if not url:
        raise ValueError(
            "HA_URL is not configured. Connect Home Assistant in the Smart Home panel."
        )
    return HomeAssistantClient(base_url=url, token=token)
