"""
Shared types, errors, and registry helpers used by both the Composio and
bespoke integration-lifecycle modules. Lives in ``_shared`` (not in the
package ``__init__``) to avoid a submodule <-> package import cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from core.plugins.registry import registry
from core.tool_router import tool_router

IntegrationStatus = Literal["available", "connected", "error"]
IntegrationKind = Literal["built_in", "composio"]
IntegrationConnection = Literal["connected", "disconnected", "unknown"]
IntegrationHealth = Literal["healthy", "degraded", "unavailable", "unknown"]


class IntegrationLifecycleError(RuntimeError):
    """Base lifecycle error with a user-facing message."""


class IntegrationUnavailableError(IntegrationLifecycleError):
    """Raised when the lifecycle operation cannot run on this instance."""


class IntegrationOperationError(IntegrationLifecycleError):
    """Raised when an external integration operation fails."""


class IntegrationConflictError(IntegrationLifecycleError):
    """Raised when the requested action conflicts with current integration state."""


class IntegrationView(BaseModel):
    model_config = {"frozen": True}

    name: str
    display_name: str
    connected: bool
    loaded: bool
    tool_count: int
    status: IntegrationStatus
    last_error: Optional[str] = None
    kind: IntegrationKind = "composio"
    enabled: bool = True
    # "composio" | "<oauth-provider>" | None — signals which reconnect flow the frontend should use
    auth_type: Optional[str] = None
    auth_providers: list[str] = Field(default_factory=list)
    description: str = ""
    connection: IntegrationConnection = "unknown"
    health: IntegrationHealth = "unknown"
    account: Optional[str] = None
    provider: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    last_used_at: Optional[datetime] = None


class DisconnectResult(BaseModel):
    model_config = {"frozen": True}

    name: str
    remote_disconnected: bool
    local_deregistered: bool


class ReconcileResult(BaseModel):
    model_config = {"frozen": True}

    name: str
    connected: bool
    loaded: bool
    message: str


def display_name(name: str) -> str:
    return name.replace("_", " ").title()


def is_toggleable(name: str) -> bool:
    """Return True if a plugin can be toggled by the user."""
    plugin = registry.plugins.get(name)
    return plugin is not None and not plugin.metadata.hidden


def local_state(name: str) -> tuple[bool, int]:
    """Return (loaded, tool_count) for a locally-registered plugin."""
    plugin = registry.plugins.get(name)
    if not plugin:
        return False, 0
    return True, len(plugin.get_tools())


async def built_in_connection_status(name: str) -> tuple[bool, str | None]:
    """Return (connected, last_error) for built-ins with external health checks."""
    if name != "smart_home":
        return True, None

    from plugins.smart_home.config import resolve_ha_connection
    from plugins.smart_home.status import check_liveness

    url, token = await resolve_ha_connection()
    try:
        liveness = await check_liveness(url, token)
    except Exception as e:
        return False, str(e)

    if liveness.authenticated:
        return True, None
    return False, liveness.message


async def deregister_local(name: str) -> bool:
    """Remove a plugin from the registry and tool router."""
    removed = await registry.deregister(name)
    tool_router.deregister_plugin(name)
    return removed
