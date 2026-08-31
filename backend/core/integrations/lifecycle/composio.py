"""
Composio-backed integration lifecycle: reconcile, list, connect-link,
disconnect, plus identity-tool discovery and caching.

Packaged mcp_servers.json is treated as an optional override layer:
  - utterances  — override auto-generated utterances
  - tools       — allowlist for large toolkits
  - triggers    — auto-register Composio triggers on connect

Any valid Composio toolkit slug can be connected regardless of whether it
has a packaged entry. The config gate has been removed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.config import settings
from core.integrations.composio_gateway import get_composio_gateway
from core.integrations.lifecycle._shared import (
    DisconnectResult,
    IntegrationConflictError,
    IntegrationOperationError,
    IntegrationStatus,
    IntegrationUnavailableError,
    IntegrationView,
    ReconcileResult,
    built_in_connection_status,
    deregister_local,
    display_name,
    local_state,
)
from core.integrations.lifecycle.bespoke import teardown_local_integration
from core.integrations.mcp.cache import load_cached_schema
from core.integrations.mcp.config import MCPConfigError, MCPServerConfig, load_mcp_config
from core.plugins.registry import registry
from services.database.mongodb import mongodb

logger = logging.getLogger(__name__)

_IDENTITY_VERBS = ("_GET_", "_RETRIEVE_")
_IDENTITY_NOUNS = ("IDENTITY", "AUTHENTICATED_USER", "CURRENT_USER", "USER_PROFILE", "_MY_")
_IDENTITY_EXCLUDED = ("LIST_", "CREATE_", "UPDATE_", "DELETE_", "SET_", "RATE_LIMIT", "PUBLIC_KEY")

# Broader patterns for fallback identity tools (e.g. SLACK_TEST_AUTH)
_FALLBACK_PATTERNS = ("_TEST_AUTH",)


def _find_identity_tools(app_name: str) -> list[str]:
    """Return zero-param identity tool candidates, best-first.

    Primary candidates match verb+noun heuristics (e.g. RETRIEVE + IDENTITY).
    Fallback candidates match broader patterns (e.g. TEST_AUTH) and are
    appended after primary ones so they're tried only if the primary fails.
    """
    tools = load_cached_schema(app_name)
    if not tools:
        return []

    primary: list[str] = []
    fallback: list[str] = []

    for tool in tools:
        name = tool.get("name", "")
        schema = tool.get("inputSchema", {})
        if schema.get("properties"):
            continue
        name_upper = name.upper()

        if any(x in name_upper for x in _IDENTITY_EXCLUDED):
            continue

        has_verb = any(v in name_upper for v in _IDENTITY_VERBS)
        has_noun = any(n in name_upper for n in _IDENTITY_NOUNS)
        if has_verb and has_noun:
            primary.append(name)
            continue

        if any(p in name_upper for p in _FALLBACK_PATTERNS):
            fallback.append(name)

    return primary + fallback


def _extract_identity_string(app_name: str, result: dict) -> str | None:
    """Extract a short identity string from a Composio identity tool response.

    Walks top-level and one level deep for common identity fields (name, email, login, id).
    """
    data = result.get("data", result)
    if not isinstance(data, dict):
        return None

    # Some responses nest user info one level deep (e.g. Slack's {"user": {...}})
    if len(data) <= 3:
        for v in data.values():
            if isinstance(v, dict) and ("name" in v or "email" in v or "login" in v or "id" in v):
                data = v
                break

    fields: list[str] = []
    for key in ("login", "user", "name", "real_name", "display_name", "email", "user_id", "id"):
        val = data.get(key)
        if val and str(val) not in fields:
            fields.append(str(val))

    if not fields:
        return None
    return f"{display_name(app_name)}: {', '.join(fields)}"


async def _fetch_and_store_identity(gateway, app_name: str) -> None:
    """Fetch the authenticated user's identity for a service and cache it.

    Tries each candidate tool in order (primary first, then fallback) until
    one returns a parseable identity. Failures are silently logged; they must
    never block the connect flow.
    """
    candidates = _find_identity_tools(app_name)
    if not candidates:
        return

    for tool_name in candidates:
        try:
            result = await gateway.execute_composio_tool(tool_name, {})
            if not result.get("successful", True):
                logger.debug(
                    "Identity tool '%s' failed for '%s': %s",
                    tool_name, app_name, result.get("error", "unknown"),
                )
                continue

            identity_str = _extract_identity_string(app_name, result)
            if not identity_str:
                logger.debug(
                    "Could not extract identity from '%s' via '%s': %s",
                    app_name, tool_name, result,
                )
                continue

            owner_id = settings.DEFAULT_USER_ID
            identities = await mongodb.get_tool_data(owner_id, "service_identities")
            identities[app_name] = identity_str
            await mongodb.store_tool_data(owner_id, "service_identities", identities)
            logger.info("Service identity cached for '%s': %s", app_name, identity_str)
            return
        except Exception as e:
            logger.debug("Identity tool '%s' raised for '%s': %s", tool_name, app_name, e)

    logger.warning("No identity tool succeeded for '%s' (tried %s)", app_name, candidates)


async def _clear_identity(app_name: str) -> None:
    """Remove a service's cached identity entry on disconnect."""
    try:
        owner_id = settings.DEFAULT_USER_ID
        identities = await mongodb.get_tool_data(owner_id, "service_identities")
        if app_name in identities:
            del identities[app_name]
            await mongodb.store_tool_data(owner_id, "service_identities", identities)
            logger.debug("Service identity cleared for '%s'", app_name)
    except Exception as e:
        logger.warning("Could not clear identity for '%s': %s", app_name, e)


def get_declared_composio_configs() -> list[MCPServerConfig]:
    """Return Composio-backed integrations declared in mcp_servers.json."""
    try:
        configs = load_mcp_config(settings.MCP_SERVERS_CONFIG)
    except MCPConfigError:
        return []
    return [config for config in configs if config.type == "composio"]


def _get_config_overrides(name: str) -> Optional[MCPServerConfig]:
    """Return the mcp_servers.json entry for a toolkit, or None if absent.

    Used to apply optional overrides (utterances, tools allowlist, triggers)
    when connecting or reconciling a toolkit. Absence is not an error.
    """
    for config in get_declared_composio_configs():
        if config.name == name:
            return config
    return None


def _require_composio_gateway():
    gateway = get_composio_gateway()
    if not gateway:
        raise IntegrationUnavailableError(
            "Composio is not configured on this instance."
        )
    return gateway


async def list_integrations() -> list[IntegrationView]:
    """Return all user-facing plugins and Composio integrations.

    Returns:
    - Built-in plugins from the registry (excluding hidden infrastructure
      and MCP-bridged plugins already shown in the composio section).
    - Packaged Composio apps (available → connected).
    - Dynamically connected Composio apps with no packaged entry.

    Sorted: built-in first (alphabetical), then connected Composio, then available.
    """
    declared_configs = get_declared_composio_configs()
    declared_names = {c.name for c in declared_configs}

    connected_apps: set[str] = set()
    connection_error: str | None = None

    gateway = get_composio_gateway()
    if gateway is None:
        connection_error = "Composio is not configured on this instance."
    else:
        try:
            connected_apps = set(await gateway.list_connected_apps())
        except Exception as e:
            logger.warning("Could not fetch connected apps from Composio: %s", e)
            connection_error = "Connection status is unavailable right now."

    # Bespoke plugins always show as built-in — strip any name collision with
    # Composio so a same-named external app doesn't shadow them.
    composio_names = (declared_names | connected_apps) - registry.bespoke_names

    # --- Built-in plugins ---
    from core.integrations.manager import integrations as integrations_mgr
    from core.auth.manager import auth_manager

    built_in_items: list[IntegrationView] = []
    for name, plugin in registry.plugins.items():
        if plugin.metadata.hidden or name in composio_names:
            continue
        tools = plugin.get_tools()

        composio_app = plugin.metadata.composio_app
        auth_missing = bool(
            composio_app
            and not connection_error
            and composio_app not in connected_apps
        )
        last_error: str | None = None
        auth_type: str | None = None
        auth_providers: list[str] = []
        connected_providers: list[str] = []

        if composio_app:
            auth_type = "composio"
            if auth_missing:
                last_error = "Auth disconnected"
        else:
            auth_providers = integrations_mgr.resolve_oauth_providers(name)
            built_in_connected, built_in_error = await built_in_connection_status(name)
            if not built_in_connected:
                auth_missing = True
                last_error = built_in_error
            elif auth_providers or name == "calendar":
                if name == "calendar":
                    from plugins.calendar.providers.eventkit import (
                        CALENDAR_ACCESS_REQUIRED,
                        macos_calendar_message,
                        macos_connection_state,
                    )

                    host_on, macos_status = await macos_connection_state()
                    if host_on:
                        auth_providers = ["macos", *auth_providers]
                    if macos_status == "authorized":
                        connected_providers.append("macos")

                for candidate in auth_providers:
                    if candidate == "macos":
                        continue
                    if await auth_manager.peek_grant(candidate):
                        connected_providers.append(candidate)

                if connected_providers:
                    auth_type = connected_providers[0]
                else:
                    auth_missing = True
                    oauth_names = [p for p in auth_providers if p != "macos"]
                    if "macos" in auth_providers:
                        last_error = (
                            macos_calendar_message(macos_status)
                            if macos_status
                            else CALENDAR_ACCESS_REQUIRED
                        )
                    elif oauth_names:
                        last_error = f"{' or '.join(p.title() for p in oauth_names)} auth required"
                    auth_type = auth_providers[0] if auth_providers else None

        built_in_items.append(
            IntegrationView(
                name=name,
                display_name=display_name(name),
                connected=not auth_missing,
                loaded=True,
                tool_count=len(tools),
                status="error" if auth_missing else "connected",
                last_error=last_error,
                kind="built_in",
                enabled=registry.is_enabled(name),
                auth_type=auth_type,
                auth_providers=auth_providers,
                connected_providers=connected_providers,
                description=plugin.metadata.description,
                connection=(
                    "unknown"
                    if composio_app and connection_error
                    else "disconnected"
                    if auth_missing
                    else "connected"
                ),
                health=(
                    "unknown"
                    if composio_app and connection_error
                    else "degraded"
                    if auth_missing
                    else "healthy"
                ),
                provider=auth_type,
                capabilities=plugin.metadata.capabilities or sorted(tools),
            )
        )
    built_in_items.sort(key=lambda i: i.name)

    items: list[IntegrationView] = []

    for config in declared_configs:
        if registry.is_bespoke(config.name):
            continue
        loaded, tool_count = local_state(config.name)
        connected = config.name in connected_apps
        status: IntegrationStatus
        last_error: str | None = None
        if connection_error:
            status = "error"
            last_error = connection_error
        elif loaded and not connected:
            status = "error"
            last_error = (
                "Remote connection is missing. Reconnect this integration to reload its tools."
            )
        elif connected:
            status = "connected"
        else:
            status = "available"

        items.append(
            IntegrationView(
                name=config.name,
                display_name=display_name(config.name),
                connected=connected,
                loaded=loaded,
                tool_count=tool_count,
                status=status,
                last_error=last_error,
                connection=(
                    "unknown" if connection_error else "connected" if connected else "disconnected"
                ),
                health=(
                    "unavailable"
                    if connection_error
                    else "degraded"
                    if loaded and not connected
                    else "healthy"
                    if connected
                    else "unknown"
                ),
                provider="composio",
                capabilities=(
                    registry.plugins[config.name].metadata.capabilities
                    or sorted(registry.plugins[config.name].get_tools())
                    if config.name in registry.plugins
                    else sorted(config.tools or [])
                ),
            )
        )

    for slug in connected_apps - declared_names:
        loaded, tool_count = local_state(slug)
        items.append(
            IntegrationView(
                name=slug,
                display_name=display_name(slug),
                connected=True,
                loaded=loaded,
                tool_count=tool_count,
                status="connected",
                last_error=connection_error,
                connection="connected",
                health="degraded" if connection_error or not loaded else "healthy",
                provider="composio",
                capabilities=(
                    registry.plugins[slug].metadata.capabilities
                    or sorted(registry.plugins[slug].get_tools())
                    if slug in registry.plugins
                    else []
                ),
            )
        )

    items.sort(key=lambda item: (not item.connected, item.name))
    return await _enrich_trust_surface(built_in_items + items)


async def _enrich_trust_surface(items: list[IntegrationView]) -> list[IntegrationView]:
    """Attach optional account and activity context without affecting lifecycle state."""
    if not items:
        return items

    owner_id = settings.DEFAULT_USER_ID
    identities = await mongodb.get_tool_data(owner_id, "service_identities")
    last_used: dict[str, datetime] = {}

    try:
        cursor = (
            mongodb.db.turn_runs.find(
                {
                    "owner_id": owner_id,
                    "tool_routing.matched_plugins": {"$in": [item.name for item in items]},
                },
                {"tool_routing.matched_plugins": 1, "completed_at": 1, "started_at": 1},
            )
            .sort("completed_at", -1)
            .limit(250)
        )
        async for run in cursor:
            used_at = run.get("completed_at") or run.get("started_at")
            for name in (run.get("tool_routing") or {}).get("matched_plugins") or []:
                if name not in last_used and isinstance(used_at, datetime):
                    last_used[name] = used_at
    except Exception as exc:
        logger.debug("Could not derive integration activity: %s", exc)

    return [
        item.model_copy(
            update={
                "account": identities.get(item.name),
                "last_used_at": last_used.get(item.name),
            }
        )
        for item in items
    ]


async def get_integration(name: str) -> IntegrationView | None:
    normalized = name.strip().lower()
    return next(
        (item for item in await list_integrations() if item.name == normalized),
        None,
    )


async def create_connect_link(name: str) -> str:
    """Generate a Composio connect link for any valid toolkit slug.

    No YAML entry required — any Composio toolkit slug is accepted as long
    as Composio is configured on this instance.
    """
    name = name.strip().lower()
    gateway = _require_composio_gateway()

    try:
        connected_apps = set(await gateway.list_connected_apps())
        if name in connected_apps:
            raise IntegrationConflictError(f"{display_name(name)} is already connected.")
        loaded, _ = local_state(name)
        if loaded:
            await teardown_local_integration(name)
        return await gateway.get_connect_link(name)
    except IntegrationConflictError:
        raise
    except Exception as e:
        logger.error("Failed to generate connect link for '%s': %s", name, e)
        raise IntegrationOperationError(
            f"Could not generate an authorization link for {name} right now."
        ) from e


async def disconnect_integration(name: str) -> DisconnectResult:
    """Revoke a remote connection, then remove the local plugin if successful.

    For Composio-backed bespoke plugins (e.g. Spotify): revokes the remote
    Composio auth only. The local plugin stays registered so it can be
    reconnected without a restart.

    For non-bespoke Composio integrations: revokes remote auth and deregisters
    the local plugin.

    Bespoke plugins without external Composio auth (e.g. toggle-only built-ins)
    cannot be disconnected here — use the toggle to disable them.
    """
    if registry.is_bespoke(name):
        plugin = registry.plugins.get(name)
        composio_app = plugin.metadata.composio_app if plugin else None
        if not composio_app:
            raise IntegrationConflictError(
                f"{display_name(name)} is a built-in plugin and cannot be disconnected here. "
                "Use the built-in toggle to disable it."
            )
        # Composio-backed bespoke: revoke remote auth + reset MCP server so
        # the next reconnect gets a fresh auth config (avoids stale ac_xxx errors).
        gateway = _require_composio_gateway()
        try:
            remote_disconnected = await gateway.disconnect_app(composio_app)
        except Exception as e:
            logger.error("Error disconnecting Composio auth for bespoke plugin '%s': %s", name, e)
            raise IntegrationOperationError(
                f"Could not disconnect {display_name(name)} right now."
            ) from e
        await _clear_identity(composio_app)
        await gateway.reset_mcp_server(composio_app)
        if plugin and hasattr(plugin, "_client"):
            plugin._client = None
        return DisconnectResult(
            name=name,
            remote_disconnected=remote_disconnected,
            local_deregistered=False,
        )

    gateway = _require_composio_gateway()

    try:
        remote_disconnected = await gateway.disconnect_app(name)
    except Exception as e:
        logger.error("Error disconnecting Composio app '%s': %s", name, e)
        raise IntegrationOperationError(
            f"Could not disconnect {name} right now."
        ) from e

    local_deregistered = await deregister_local(name)
    await _clear_identity(name)
    return DisconnectResult(
        name=name,
        remote_disconnected=remote_disconnected,
        local_deregistered=local_deregistered,
    )


async def reconcile_integration(name: str) -> ReconcileResult:
    """
    Reconcile one Composio integration with authoritative remote state.

    Applies optional YAML overrides (utterances, tools allowlist, triggers)
    when present, but proceeds without them if no YAML entry exists.
    """
    name = name.strip().lower()
    gateway = _require_composio_gateway()
    loaded, _ = local_state(name)

    try:
        connected_apps = set(await gateway.list_connected_apps())
    except Exception as e:
        logger.error("Could not reconcile integration '%s': %s", name, e)
        raise IntegrationOperationError(
            "Could not verify the remote connection status right now."
        ) from e

    if name not in connected_apps:
        if loaded:
            await teardown_local_integration(name)
        return ReconcileResult(
            name=name,
            connected=False,
            loaded=False,
            message=f"{display_name(name)} is not connected.",
        )

    if loaded:
        return ReconcileResult(
            name=name,
            connected=True,
            loaded=True,
            message=f"{display_name(name)} is already loaded.",
        )

    config = _get_config_overrides(name)
    try:
        loaded = await gateway.on_app_connected(
            name,
            tools_allowlist=config.tools if config else None,
            utterances_override=config.utterances if config else None,
            triggers=config.triggers if config else None,
        )
    except Exception as e:
        logger.error("Could not load tools for integration '%s': %s", name, e)
        raise IntegrationOperationError(
            f"Could not load tools for {name} right now."
        ) from e

    if loaded:
        await _fetch_and_store_identity(gateway, name)

    return ReconcileResult(
        name=name,
        connected=True,
        loaded=loaded,
        message=(
            f"{display_name(name)} is ready to use."
            if loaded
            else f"{display_name(name)} is connected but its tools are not loaded yet."
        ),
    )


async def reconcile_composio_startup() -> list[ReconcileResult]:
    """Reconcile ALL connected Composio apps with the local plugin registry.

    Discovers connected apps from the Composio API (not just packaged entries),
    so apps connected in a previous session are loaded even without a config entry.
    Reconciles concurrently for speed.
    """
    gateway = get_composio_gateway()
    if not gateway:
        return []

    try:
        connected_full = await gateway.list_all_connected_apps_full()
    except Exception as e:
        logger.warning("Startup: could not list connected Composio apps: %s", e)
        return []

    if not connected_full:
        return []

    async def _safe_reconcile(name: str) -> ReconcileResult | None:
        try:
            return await reconcile_integration(name)
        except Exception as e:
            logger.warning("Startup reconcile failed for '%s': %s", name, e)
            return None

    results = await asyncio.gather(*[_safe_reconcile(a["slug"]) for a in connected_full])
    return [r for r in results if r is not None]
