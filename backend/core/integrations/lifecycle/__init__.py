"""
Integration lifecycle public API.

Keeps integration connection/load workflows out of delivery layers such as
REST routes and voice-facing plugins. Submodules:

- `composio`: reconcile/connect/disconnect for Composio-backed integrations
- `bespoke`:  non-Composio MCP bridges + local plugin teardown
- `_shared`:  cross-submodule types, errors, and registry helpers
"""

from core.integrations.lifecycle._shared import (
    DisconnectResult,
    IntegrationConflictError,
    IntegrationLifecycleError,
    IntegrationOperationError,
    IntegrationUnavailableError,
    IntegrationView,
    ReconcileResult,
    is_toggleable,
)
from core.integrations.lifecycle.bespoke import (
    refresh_non_composio_integrations,
    teardown_local_integration,
)
from core.integrations.lifecycle.composio import (
    create_connect_link,
    disconnect_integration,
    get_declared_composio_configs,
    get_integration,
    list_integrations,
    reconcile_composio_startup,
    reconcile_integration,
)

__all__ = [
    "DisconnectResult",
    "IntegrationConflictError",
    "IntegrationLifecycleError",
    "IntegrationOperationError",
    "IntegrationUnavailableError",
    "IntegrationView",
    "ReconcileResult",
    "create_connect_link",
    "disconnect_integration",
    "get_declared_composio_configs",
    "get_integration",
    "is_toggleable",
    "list_integrations",
    "reconcile_composio_startup",
    "reconcile_integration",
    "refresh_non_composio_integrations",
    "teardown_local_integration",
]
