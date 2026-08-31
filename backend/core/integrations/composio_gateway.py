"""
Composio Gateway.

Manages Composio-backed integrations: Connect Links, OAuth callbacks, per-app
MCP tool registration, hot-reload into the plugin registry and Tool Router.

Composio provides pre-registered OAuth apps for 500+ services. The user
never visits a developer console — they click a generated Connect Link,
authorizes via Composio's OAuth app, and the tools appear immediately.

Architecture:
  - One Composio user_id per JARV1S user (mapped by DEFAULT_USER_ID from settings)
  - One MCP server config per toolkit (persistent on Composio, created on first use)
  - Tools bridged into jarvis.<app_name>.* via MCPBridgePlugin + StreamableHTTPClient
  - Hot-reload: callback fires → discover tools → register plugin → update ToolRouter
  - mcp_servers.json is optional: overrides utterances/allowlist/triggers per toolkit

Prerequisites:
  - COMPOSIO_API_KEY in CredentialStore

References:
  https://docs.composio.dev/reference/api-reference
"""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from core.auth.oauth_flow import issue_callback_nonce
from core.config import settings
from core.credentials.store import credential_store
from core.integrations.mcp.bridge import (
    MCPBridgePlugin,
    generate_utterances,
)
from core.integrations.mcp.cache import save_schema_cache
from core.integrations.mcp.http_client import StreamableHTTPClient
from core.plugins.registry import registry
from core.tool_router import tool_router

logger = logging.getLogger(__name__)

_COMPOSIO_BASE = "https://backend.composio.dev/api/v3.1"
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # seconds; multiplied by attempt number

# Composio exposes "new-style" trigger slugs in the global enum that subscribe
# without error but never deliver webhooks. Filter them from the LLM's options
# so it picks the working legacy SLACK_RECEIVE_* slugs instead. Confirmed by
# inspecting inbound_events: these slugs produce zero deliveries while the
# legacy equivalents fire reliably.
_BROKEN_TRIGGER_SLUGS: frozenset[str] = frozenset(
    {
        "SLACK_DIRECT_MESSAGE_RECEIVED",  # use SLACK_RECEIVE_DIRECT_MESSAGE
        "SLACK_CHANNEL_MESSAGE_RECEIVED",  # use SLACK_RECEIVE_MESSAGE
        "SLACK_PRIVATE_MESSAGE_POSTED",  # use SLACK_RECEIVE_GROUP_MESSAGE
    }
)


class ComposioGateway:
    """
    Manages the Composio integration lifecycle for JARV1S.

    Provides Connect Links, handles OAuth callbacks, and hot-reloads
    newly connected toolkit tools into the running plugin registry and ToolRouter.

    v3 concepts:
      - Auth Config (ac_xxxx): per-toolkit auth blueprint, created once and reused.
      - MCP Server Config: per-toolkit MCP server, created once and reused.
    Both are lazy-created on first use and cached in memory.
    """

    def __init__(
        self,
        api_key: str,
        user_id: str,
        callback_host: str,
        frontend_origin: str,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._callback_host = callback_host  # public URL for webhook delivery
        self._frontend_origin = frontend_origin  # used for OAuth callback URL
        self._client = httpx.AsyncClient(
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
        # In-memory caches — populated lazily, re-fetched on restart
        self._auth_configs: dict[str, str] = {}  # toolkit -> ac_xxxx
        self._mcp_servers: dict[str, str] = {}  # toolkit -> server UUID
        self._mcp_clients: dict[str, StreamableHTTPClient] = {}  # toolkit -> MCP client
        self._trigger_types: dict[str, list[dict]] = {}  # toolkit -> trigger type items
        self._webhook_secret: Optional[str] = None

    @property
    def webhook_secret(self) -> Optional[str]:
        """HMAC secret for verifying Composio trigger delivery signatures."""
        if self._webhook_secret:
            return self._webhook_secret
        stored = credential_store.get_secret("COMPOSIO_WEBHOOK_SECRET")
        if stored:
            self._webhook_secret = stored
            return stored
        return settings.COMPOSIO_WEBHOOK_SECRET

    def set_callback_host(self, callback_host: str) -> None:
        """Hot-update the public callback origin used for webhook registration."""
        self._callback_host = callback_host.rstrip("/")

    def _persist_webhook_secret(self, secret: str | None) -> None:
        if not secret:
            return
        self._webhook_secret = secret
        try:
            credential_store.set_secret("COMPOSIO_WEBHOOK_SECRET", secret)
        except Exception:
            logger.warning("Failed to persist COMPOSIO_WEBHOOK_SECRET", exc_info=True)

    async def shutdown(self) -> None:
        """Close the persistent HTTP client."""
        for client in self._mcp_clients.values():
            await client.shutdown()
        self._mcp_clients.clear()
        await self._client.aclose()

    # -----------------------------------------------------------------------
    # Private: HTTP with retry on transient errors
    # -----------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send an HTTP request, retrying on 429/5xx up to _MAX_RETRIES."""
        response: Optional[httpx.Response] = None
        for attempt in range(_MAX_RETRIES):
            response = await self._client.request(method, url, **kwargs)
            if (
                response.status_code in (429, 502, 503, 504)
                and attempt < _MAX_RETRIES - 1
            ):
                wait = _RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "Composio %s %s → %d, retrying in %.0fs",
                    method,
                    url,
                    response.status_code,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            return response
        assert response is not None
        return response

    # -----------------------------------------------------------------------
    # Private: lazy resource creation
    # -----------------------------------------------------------------------

    async def _ensure_auth_config(self, toolkit: str) -> str:
        """Return the auth config ID for a toolkit, creating one if needed."""
        if toolkit in self._auth_configs:
            return self._auth_configs[toolkit]

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/auth_configs",
            params={"toolkit_slug": toolkit},
        )
        response.raise_for_status()
        items = response.json().get("items", [])

        if items:
            ac_id = items[0]["id"]
            ac_type = items[0].get("type", "?")
            logger.debug(
                "Composio auth config for '%s': id=%s type=%s",
                toolkit,
                ac_id,
                ac_type,
            )
        else:
            response = await self._request(
                "POST",
                f"{_COMPOSIO_BASE}/auth_configs",
                json={
                    "toolkit": {"slug": toolkit},
                    "auth_config": {
                        "name": f"{toolkit.title()} Auth",
                        "type": "use_composio_managed_auth",
                    },
                },
            )
            if response.status_code == 400:
                body = (
                    response.json()
                    if response.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                slug = body.get("error", {}).get("slug", "")
                if "DefaultAuthConfigNotFound" in slug:
                    raise RuntimeError(
                        f"{toolkit.title()} requires custom OAuth credentials (client_id & client_secret). "
                        f"Composio does not provide managed auth for this service."
                    )
                response.raise_for_status()
            response.raise_for_status()
            ac_id = response.json()["auth_config"]["id"]
            logger.info("Composio auth config created for '%s': %s", toolkit, ac_id)

        self._auth_configs[toolkit] = ac_id
        return ac_id

    async def _delete_mcp_server(self, server_id: str, toolkit: str) -> bool:
        """Delete an MCP server from Composio. Best-effort — logs on failure."""
        response = await self._request("DELETE", f"{_COMPOSIO_BASE}/mcp/{server_id}")
        if response.status_code in (200, 204):
            logger.info("Composio MCP server deleted for '%s': %s", toolkit, server_id)
            return True
        logger.warning(
            "Could not delete Composio MCP server '%s' for toolkit '%s': %d %s",
            server_id,
            toolkit,
            response.status_code,
            response.text[:200] if len(response.text) > 200 else response.text,
        )
        return False

    async def _patch_mcp_server(
        self, server_id: str, toolkit: str, auth_config_ids: list[str]
    ) -> bool:
        """Update an MCP server's auth config on Composio. Returns True on success."""
        response = await self._request(
            "PATCH",
            f"{_COMPOSIO_BASE}/mcp/{server_id}",
            json={"auth_config_ids": auth_config_ids},
        )
        if response.is_success:
            logger.info(
                "Composio MCP server '%s' patched for '%s': auth_config_ids=%s",
                server_id,
                toolkit,
                auth_config_ids,
            )
            return True
        logger.warning(
            "Could not patch Composio MCP server '%s' for '%s': %d %s",
            server_id,
            toolkit,
            response.status_code,
            response.text[:300] if len(response.text) > 300 else response.text,
        )
        return False

    async def _ensure_mcp_server(self, toolkit: str) -> str:
        """Return the MCP server ID for a toolkit, creating one if needed.

        If an existing server is found but its auth_config_ids don't match the
        current auth config (stale from a previous deployment or API key change),
        the old server is deleted and a fresh one created.
        """
        if toolkit in self._mcp_servers:
            return self._mcp_servers[toolkit]

        ac_id = await self._ensure_auth_config(toolkit)

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/mcp/servers",
            params={"toolkits": toolkit},
        )
        if response.status_code == 404:
            existing = []
        else:
            response.raise_for_status()
            existing = response.json().get("items", [])

        if existing:
            server = existing[0]
            server_id = server["id"]
            server_auth_configs = set(server.get("auth_config_ids") or [])
            logger.info(
                "Composio MCP server lookup for '%s': id=%s auth_config_ids=%s expected_ac=%s",
                toolkit,
                server_id,
                server_auth_configs,
                ac_id,
            )

            if ac_id not in server_auth_configs:
                logger.warning(
                    "Composio MCP server '%s' for '%s' has stale auth config %s "
                    "(current: %s) — patching.",
                    server_id,
                    toolkit,
                    server_auth_configs,
                    ac_id,
                )
                patched = await self._patch_mcp_server(server_id, toolkit, [ac_id])
                if not patched:
                    logger.warning(
                        "Patch failed, deleting and recreating MCP server for '%s'",
                        toolkit,
                    )
                    await self._delete_mcp_server(server_id, toolkit)
                    existing = []

        if not existing:
            response = await self._request(
                "POST",
                f"{_COMPOSIO_BASE}/mcp/servers",
                json={
                    "name": f"jarvis-{toolkit.replace('_', '-')}",
                    "auth_config_ids": [ac_id],
                },
            )
            if not response.is_success:
                logger.error(
                    "Composio MCP server creation FAILED for '%s': %d %s",
                    toolkit,
                    response.status_code,
                    response.text[:300],
                )
            response.raise_for_status()
            server_id = response.json()["id"]
            logger.info("Composio MCP server created for '%s': %s", toolkit, server_id)

        self._mcp_servers[toolkit] = server_id
        return server_id

    async def reset_mcp_server(self, toolkit: str) -> None:
        """Force-purge and rebuild the MCP server + auth config for a toolkit.

        Use when the existing Composio MCP server or auth config is stale
        (e.g. after an API key change or Docker re-deployment). Evicts the
        in-memory caches and deletes remote resources so the next connect
        creates them fresh.
        """
        logger.info("Resetting Composio MCP server and auth config for '%s'", toolkit)

        # Evict memory caches so _ensure_* re-queries the API
        self._auth_configs.pop(toolkit, None)
        old_server_id = self._mcp_servers.pop(toolkit, None)

        # Collect server IDs to delete: cached + any found via API
        server_ids: set[str] = set()
        if old_server_id:
            server_ids.add(old_server_id)

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/mcp/servers",
            params={"toolkits": toolkit},
        )
        if response.status_code == 200:
            for server in response.json().get("items", []):
                server_ids.add(server["id"])

        for sid in server_ids:
            await self._delete_mcp_server(sid, toolkit)

    def _callback_url(self, app_name: str) -> str:
        # Routed through the frontend proxy so postMessage origin check passes.
        base = f"{self._frontend_origin}{settings.API_V1_STR}/auth/composio/callback"
        return f"{base}?{urlencode({'state': issue_callback_nonce(app_name)})}"

    # -----------------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------------

    async def get_connect_link(self, app_name: str) -> str:
        """Generate a hosted Connect Link URL for the given Composio toolkit.

        Uses the /connected_accounts/link endpoint which returns a Composio-hosted
        page (connect.composio.dev/link/...). That page collects any required fields
        (e.g. Jira subdomain) automatically before starting the OAuth flow — no need
        to pass them upfront from JARV1S.
        """
        auth_config_id = await self._ensure_auth_config(app_name)

        response = await self._request(
            "POST",
            f"{_COMPOSIO_BASE}/connected_accounts/link",
            json={
                "auth_config_id": auth_config_id,
                "user_id": self._user_id,
                "callback_url": self._callback_url(app_name),
            },
        )
        if not response.is_success:
            body = response.text
            logger.error(
                "Composio connect link request failed for '%s': %d %s",
                app_name,
                response.status_code,
                body,
            )
            response.raise_for_status()

        data = response.json()
        connect_url = data.get("redirect_url")
        if not connect_url:
            raise RuntimeError(
                f"Composio did not return a connect URL for toolkit '{app_name}': {data}"
            )
        logger.info("Composio connect link generated for toolkit '%s'", app_name)
        return connect_url

    async def list_connected_apps(self) -> list[str]:
        """Return the names of all connected Composio toolkits for this user."""
        accounts = await self.list_connected_accounts()
        return sorted(
            {
                a.get("toolkit", {}).get("slug", "")
                for a in accounts
                if a.get("toolkit", {}).get("slug", "")
            }
        )

    async def list_all_connected_apps_full(self) -> list[dict]:
        """Return full details for all active connected Composio accounts for this user.

        Each dict contains at minimum: toolkit slug, display name, and account id.
        Used by reconcile_composio_startup() to discover ALL connected apps without
        requiring a YAML declaration.
        """
        accounts = await self.list_connected_accounts()
        results: list[dict] = []
        seen: set[str] = set()
        for account in accounts:
            slug = account.get("toolkit", {}).get("slug", "")
            if slug and slug not in seen:
                seen.add(slug)
                results.append(
                    {
                        "slug": slug,
                        "display_name": account.get("toolkit", {}).get("name", slug),
                        "account_id": account.get("id", ""),
                    }
                )
        return sorted(results, key=lambda x: x["slug"])

    async def search_toolkits(self, query: str = "") -> list[dict]:
        """Search available Composio toolkits by name or keyword.

        Returns a list of dicts with: slug, display_name, description, auth_type.
        Powers the frontend catalog browse/search UI. Returns all toolkits when
        query is empty.
        """
        params: dict = {"limit": 50}
        if query:
            params["search"] = query

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/toolkits",
            params=params,
        )
        if response.status_code != 200:
            logger.warning(
                "Composio toolkit search failed: %d %s",
                response.status_code,
                response.text,
            )
            return []

        items = response.json().get("items", [])
        return [
            {
                "slug": item.get("slug", ""),
                "display_name": item.get("name", item.get("slug", "")),
                "description": item.get("meta", {}).get("description", ""),
                "auth_type": (item.get("composio_managed_auth_schemes") or [""])[0]
                if not item.get("no_auth")
                else "none",
                "managed_auth": bool(item.get("composio_managed_auth_schemes")),
                "no_auth": bool(item.get("no_auth")),
            }
            for item in items
            if item.get("slug")
        ]

    async def list_connected_accounts(
        self, app_name: Optional[str] = None
    ) -> list[dict]:
        """Return active connected accounts for this user, optionally filtered by toolkit."""
        params = {"user_ids": self._user_id, "statuses": "ACTIVE"}
        if app_name:
            params["toolkit_slugs"] = app_name

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/connected_accounts",
            params=params,
        )
        response.raise_for_status()
        return response.json().get("items", [])

    async def get_mcp_url(self, app_name: str) -> Optional[str]:
        """Return the per-user MCP server URL for a connected Composio toolkit.

        Creates the toolkit's MCP server config on Composio if it doesn't exist,
        then generates a user-scoped URL from it.
        """
        server_id = await self._ensure_mcp_server(app_name)

        response = await self._request(
            "POST",
            f"{_COMPOSIO_BASE}/mcp/servers/generate",
            json={
                "mcp_server_id": server_id,
                "user_ids": [self._user_id],
            },
        )
        if not response.is_success:
            logger.error(
                "Composio MCP URL generate FAILED for '%s': %d %s",
                app_name,
                response.status_code,
                response.text[:300],
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()

        urls = data.get("user_ids_url", [])
        mcp_url = urls[0] if urls else data.get("mcp_url")
        logger.info("Composio MCP URL for '%s': %s", app_name, mcp_url)
        return mcp_url

    async def get_mcp_client(self, app_name: str) -> Optional[StreamableHTTPClient]:
        """Return a started MCP client for bespoke Composio-backed plugins."""
        cached = self._mcp_clients.get(app_name)
        if cached is not None:
            return cached

        mcp_url = await self.get_mcp_url(app_name)
        if not mcp_url:
            return None

        client = StreamableHTTPClient(
            url=mcp_url,
            headers={"x-api-key": self._api_key},
        )
        await client.start()
        self._mcp_clients[app_name] = client
        return client

    async def call_mcp_tool(
        self, app_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """Call a tool on a connected Composio toolkit's MCP server."""
        client = await self.get_mcp_client(app_name)
        if client is None:
            return {
                "error": f"{app_name.title()} not connected — connect via the Tools panel"
            }
        return await client.call_tool(tool_name, arguments)

    async def resolve_app_name(self, connected_account_id: str) -> Optional[str]:
        """Look up the toolkit name for a given Composio connected account ID."""
        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/connected_accounts/{connected_account_id}",
        )
        response.raise_for_status()
        data = response.json()
        toolkit = data.get("toolkit")
        if isinstance(toolkit, dict):
            return toolkit.get("slug")
        return toolkit or data.get("appName")

    async def disconnect_app(self, app_name: str) -> bool:
        """Revoke all active Composio connected accounts for one toolkit.

        Returns True if disconnected, False if not connected.
        Does NOT touch the local registry — callers handle deregistration.
        """
        accounts = await self.list_connected_accounts(app_name)

        if not accounts:
            logger.info("Composio toolkit '%s' is not connected", app_name)
            return False

        for account in accounts:
            account_id = account["id"]
            response = await self._request(
                "DELETE",
                f"{_COMPOSIO_BASE}/connected_accounts/{account_id}",
            )
            response.raise_for_status()

        logger.info(
            "Composio toolkit '%s' disconnected (%d connected account(s))",
            app_name,
            len(accounts),
        )
        return True

    # -----------------------------------------------------------------------
    # Trigger lifecycle
    # -----------------------------------------------------------------------

    async def ensure_webhook_subscription(self) -> None:
        """Register or update the JARV1S webhook with Composio.

        Idempotent. Persists the HMAC secret in CredentialStore so restarts
        can verify signatures without relying on env. Uses PATCH when the URL
        changes, and rotate_secret when a matching subscription exists but the
        local secret is missing.
        """
        webhook_url = f"{self._callback_host}{settings.API_V1_STR}/webhooks/composio"
        enabled_events = [
            "composio.trigger.message",
            "composio.connected_account.expired",
        ]

        response = await self._request("GET", f"{_COMPOSIO_BASE}/webhook_subscriptions")
        existing: dict | None = None
        if response.status_code == 200:
            items = response.json().get("items") or []
            if items:
                existing = items[0]

        if existing:
            sub_id = existing.get("id")
            current_url = existing.get("webhook_url")
            if sub_id and current_url != webhook_url:
                patch = await self._request(
                    "PATCH",
                    f"{_COMPOSIO_BASE}/webhook_subscriptions/{sub_id}",
                    json={"webhook_url": webhook_url, "enabled_events": enabled_events},
                )
                if patch.status_code not in (200, 201):
                    logger.warning(
                        "Failed to update Composio webhook URL: %d %s",
                        patch.status_code,
                        patch.text,
                    )
                    return
                data = patch.json()
                self._persist_webhook_secret(data.get("secret") or self.webhook_secret)
                logger.info("Composio webhook subscription updated to %s", webhook_url)
                return

            if self.webhook_secret:
                logger.info("Composio webhook subscription ready at %s", webhook_url)
                return

            if not sub_id:
                logger.warning("Composio webhook subscription missing id; cannot rotate secret")
                return
            rotated = await self._request(
                "POST",
                f"{_COMPOSIO_BASE}/webhook_subscriptions/{sub_id}/rotate_secret",
            )
            if rotated.status_code not in (200, 201):
                logger.warning(
                    "Failed to rotate Composio webhook secret: %d %s",
                    rotated.status_code,
                    rotated.text,
                )
                return
            self._persist_webhook_secret(rotated.json().get("secret"))
            logger.info("Composio webhook secret rotated for %s", webhook_url)
            return

        response = await self._request(
            "POST",
            f"{_COMPOSIO_BASE}/webhook_subscriptions",
            json={
                "webhook_url": webhook_url,
                "enabled_events": enabled_events,
                "version": "V3",
            },
        )
        if response.status_code not in (200, 201):
            logger.warning(
                "Failed to register Composio webhook subscription: %d %s",
                response.status_code,
                response.text,
            )
            return

        data = response.json()
        self._persist_webhook_secret(data.get("secret"))
        logger.info("Composio webhook subscription registered at %s", webhook_url)

    async def clear_webhook_subscription(self) -> None:
        """Best-effort delete of the project webhook subscription."""
        response = await self._request("GET", f"{_COMPOSIO_BASE}/webhook_subscriptions")
        if response.status_code != 200:
            return
        for sub in response.json().get("items") or []:
            sub_id = sub.get("id")
            if not sub_id:
                continue
            deleted = await self._request(
                "DELETE",
                f"{_COMPOSIO_BASE}/webhook_subscriptions/{sub_id}",
            )
            if deleted.status_code not in (200, 204):
                logger.warning(
                    "Failed to delete Composio webhook subscription %s: %d %s",
                    sub_id,
                    deleted.status_code,
                    deleted.text,
                )
        self._webhook_secret = None
        try:
            credential_store.delete_secret("COMPOSIO_WEBHOOK_SECRET")
        except Exception:
            logger.debug("Failed to clear stored COMPOSIO_WEBHOOK_SECRET", exc_info=True)

    async def register_trigger(
        self,
        app_name: str,
        trigger_slug: str,
        config: Optional[dict] = None,
    ) -> bool:
        """Enable a Composio trigger instance for the user's connected account.

        Called automatically in on_app_connected for slugs declared in
        mcp_servers.json (or passed as the triggers parameter), and
        on-demand via the LLM-facing enable_trigger tool.
        """
        accounts = await self.list_connected_accounts(app_name)
        if not accounts:
            logger.warning(
                "Cannot register trigger '%s' for '%s': no active connected account",
                trigger_slug,
                app_name,
            )
            return False

        connected_account_id = accounts[0]["id"]

        response = await self._request(
            "POST",
            f"{_COMPOSIO_BASE}/trigger_instances/{trigger_slug}/upsert",
            json={
                "connected_account_id": connected_account_id,
                "trigger_config": config or {},
            },
        )
        if response.status_code not in (200, 201):
            logger.warning(
                "Failed to register Composio trigger '%s' for toolkit '%s': %d %s",
                trigger_slug,
                app_name,
                response.status_code,
                response.text,
            )
            return False

        logger.info(
            "Composio trigger '%s' registered for toolkit '%s'",
            trigger_slug,
            app_name,
        )
        return True

    async def list_trigger_types(self, app_name: str) -> list[dict]:
        """Return available trigger types for a Composio toolkit.

        Composio v3.1's `?toolkit_slugs=<x>` filter is incomplete — it omits
        several real triggers (e.g. SLACK_RECEIVE_DIRECT_MESSAGE,
        SLACK_RECEIVE_THREAD_REPLY). We union the filter response with the
        global `/triggers_types/list/enum` slugs prefixed `<TOOLKIT>_`,
        fetching detail per missing slug. Result is cached per toolkit.
        """
        cached = self._trigger_types.get(app_name)
        if cached is not None:
            return cached

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/triggers_types",
            params={"toolkit_slugs": app_name},
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to list trigger types for toolkit '%s': %d %s",
                app_name,
                response.status_code,
                response.text,
            )
            return []
        items: list[dict] = [
            it
            for it in response.json().get("items", [])
            if (it.get("slug") or it.get("name") or "") not in _BROKEN_TRIGGER_SLUGS
        ]
        seen = {(it.get("slug") or it.get("name") or "") for it in items}

        prefix = f"{app_name.upper()}_"
        enum_resp = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/triggers_types/list/enum",
        )
        if enum_resp.status_code == 200:
            enum_slugs = enum_resp.json()
            missing = [
                s
                for s in enum_slugs
                if s.startswith(prefix)
                and s not in seen
                and s not in _BROKEN_TRIGGER_SLUGS
            ]
            for slug in missing:
                detail = await self._request(
                    "GET",
                    f"{_COMPOSIO_BASE}/triggers_types/{slug}",
                )
                if detail.status_code != 200:
                    continue
                d = detail.json()
                # Only keep triggers that actually belong to this toolkit.
                tk = d.get("toolkit") or {}
                tk_slug = tk.get("slug") if isinstance(tk, dict) else tk
                if tk_slug and tk_slug.lower() != app_name.lower():
                    continue
                items.append(d)
            if missing:
                logger.info(
                    "Composio list_trigger_types('%s'): added %d missing triggers via enum fallback (%s)",
                    app_name,
                    len(missing),
                    missing,
                )

        self._trigger_types[app_name] = items
        return items

    async def disable_trigger(self, trigger_id: str) -> bool:
        """Disable a Composio trigger instance by its trigger instance ID."""
        response = await self._request(
            "DELETE",
            f"{_COMPOSIO_BASE}/trigger_instances/manage/{trigger_id}",
        )
        if response.status_code not in (200, 204):
            logger.warning(
                "Failed to disable Composio trigger '%s': %d %s",
                trigger_id,
                response.status_code,
                response.text,
            )
            return False
        logger.info("Composio trigger '%s' disabled", trigger_id)
        return True

    async def deregister_trigger(self, app_name: str, trigger_slug: str) -> bool:
        """Disable all active trigger instances for a given trigger slug + app.

        Fetches active instances filtered by trigger type slug and toolkit, then
        deletes each one. Called automatically by delete_rule cleanup.
        """
        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/trigger_instances",
            params={"triggerTypeSlug": trigger_slug, "toolkit": app_name},
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to list trigger instances for '%s'/'%s': %d %s",
                app_name,
                trigger_slug,
                response.status_code,
                response.text,
            )
            return False

        items = response.json().get("items", [])
        if not items:
            return True

        success = True
        for item in items:
            instance_id = item.get("id")
            if instance_id:
                ok = await self.disable_trigger(instance_id)
                if not ok:
                    success = False
        return success

    # Tool search & execution (used by ComposioMetaPlugin overflow tools)
    # -----------------------------------------------------------------------

    async def search_composio_tools(
        self, query: str, toolkit_slug: Optional[str] = None, limit: int = 10
    ) -> list[dict]:
        """Search Composio's tool catalog via the V3 REST API.

        Correctly uses 'query' (not 'q') and scopes to a specific toolkit when
        provided. Does NOT accept user_id — the API is scoped by API key only.
        """
        params: dict[str, Any] = {"query": query, "limit": limit}
        if toolkit_slug:
            params["toolkit_slug"] = toolkit_slug

        response = await self._request(
            "GET",
            f"{_COMPOSIO_BASE}/tools",
            params=params,
        )
        if response.status_code != 200:
            logger.warning(
                "Composio tool search failed: %d %s",
                response.status_code,
                response.text,
            )
            return []

        return [
            {
                "name": item.get("slug", item.get("name", "")),
                "description": item.get("description", ""),
                "app": item.get("toolkit", {}).get("slug", ""),
            }
            for item in response.json().get("items", [])
            if item.get("slug") or item.get("name")
        ]

    async def execute_composio_tool(
        self, tool_name: str, params: dict[str, Any]
    ) -> dict:
        """Execute a Composio tool by name with the user's credentials.

        Uses v3.1 endpoint: POST /api/v3.1/tools/execute/{tool_slug}
        """
        response = await self._request(
            "POST",
            f"{_COMPOSIO_BASE}/tools/execute/{tool_name}",
            json={
                "user_id": self._user_id,
                "arguments": params,
            },
        )
        if response.status_code not in (200, 201):
            return {
                "error": f"Tool execution failed: {response.status_code}",
                "detail": response.text,
            }
        return response.json()

    # -----------------------------------------------------------------------
    # Hot-reload: called from the OAuth callback endpoint
    # -----------------------------------------------------------------------

    async def on_app_connected(
        self,
        app_name: str,
        tools_allowlist: Optional[list[str]] = None,
        utterances_override: Optional[list[str]] = None,
        triggers: Optional[list[str]] = None,
    ) -> bool:
        """Discover and register tools for a newly connected Composio toolkit.

        Called when the OAuth callback fires after successful user authorization,
        or during startup reconciliation. Hot-reloads tools into the live registry
        + ToolRouter without restart.

        Optional overrides (from mcp_servers.json via lifecycle._get_config_overrides):
          - tools_allowlist: host-owned allowlist — only these tools are mounted.
            Missing or empty mounts nothing (fail-closed). User Home mcp.json is not Composio.
          - utterances_override: if provided, replaces auto-generated utterances
          - triggers: if provided, these Composio triggers are auto-registered

        ALL mounted tools are callable via jarvis.<app>.*. Tool routing and the
        per-turn tools= set are handled by ToolRouter using hybrid utterance + description embeddings.
        """
        if not tools_allowlist:
            logger.warning(
                "Composio '%s': no tools allowlist; mounting nothing",
                app_name,
            )
            return False

        mcp_url = await self.get_mcp_url(app_name)
        if not mcp_url:
            logger.warning(
                "Composio toolkit '%s' connected but no MCP URL available",
                app_name,
            )
            return False

        client = StreamableHTTPClient(
            url=mcp_url,
            headers={"x-api-key": self._api_key},
        )
        try:
            await client.start()
            raw_tools = await client.list_tools()
        except Exception as e:
            logger.error(
                "Failed to fetch tools for Composio toolkit '%s' (url=%s): %s",
                app_name,
                mcp_url,
                e,
            )
            await client.shutdown()
            return False

        save_schema_cache(app_name, raw_tools)

        tools = [t for t in raw_tools if t["name"] in tools_allowlist]
        logger.debug(
            "Composio '%s': allowlist filtered to %d / %d tools",
            app_name,
            len(tools),
            len(raw_tools),
        )

        # Derive utterances from ALL fetched tools for richer routing signals.
        utterances = utterances_override or generate_utterances(raw_tools, app_name)

        plugin = MCPBridgePlugin(
            server_name=app_name,
            client=client,
            all_tools=tools,
            utterances=utterances,
        )

        registered = await registry.register(plugin)
        if not registered:
            logger.info(
                "Composio toolkit '%s' — bespoke plugin takes priority",
                app_name,
            )
            await client.shutdown()
            return False

        await tool_router.register_plugin(
            app_name, plugin.get_tools(), utterances=utterances
        )

        logger.info(
            "Composio toolkit '%s' hot-loaded: %s.* (%d tools)",
            app_name,
            app_name,
            len(plugin.get_tools()),
        )

        if triggers:
            for trigger_slug in triggers:
                await self.register_trigger(app_name, trigger_slug)

        return True


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_gateway: Optional["ComposioGateway"] = None


async def reset_composio_gateway() -> None:
    """Close and drop cached gateway so the next access rebuilds from current credentials."""
    global _gateway
    gateway = _gateway
    _gateway = None
    if gateway is not None:
        await gateway.shutdown()


def get_composio_gateway() -> Optional["ComposioGateway"]:
    """Return the global ComposioGateway instance if configured, else None."""
    global _gateway
    if _gateway is not None:
        return _gateway

    api_key = credential_store.get_stored_secret("COMPOSIO_API_KEY")
    if not api_key:
        return None

    from core.integrations.external_ingress import resolve_external_ingress_base_url_sync

    callback_host = (
        resolve_external_ingress_base_url_sync()
        or getattr(settings, "EXTERNAL_INGRESS_BASE_URL", None)
        or "http://127.0.0.1:8000"
    )
    _gateway = ComposioGateway(
        api_key=api_key,
        user_id=settings.DEFAULT_USER_ID,
        callback_host=str(callback_host).rstrip("/"),
        frontend_origin=settings.FRONTEND_ORIGIN,
    )
    return _gateway
