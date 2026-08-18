"""Connect-time widget snapshot providers.

Domain services remain the source of truth. Providers rebuild active
``UIEnvelope`` instances from those stores when a display reconnects.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

from core.plugins.types import UIEnvelope

logger = logging.getLogger(__name__)

WidgetSnapshotProvider = Callable[[str], Awaitable[list[UIEnvelope]] | list[UIEnvelope]]

_providers: dict[str, WidgetSnapshotProvider] = {}
_builtins_loaded = False


def register_widget_snapshot_provider(name: str, provider: WidgetSnapshotProvider) -> None:
    """Register a domain-owned provider for active widget snapshots."""
    _providers[name] = provider


async def collect_widget_snapshots(owner_id: str) -> list[UIEnvelope]:
    """Collect active widgets for an owner, isolating provider failures."""
    _ensure_builtin_providers()
    snapshots_by_id: dict[str, UIEnvelope] = {}
    for name, provider in list(_providers.items()):
        try:
            result = provider(owner_id)
            widgets = await result if inspect.isawaitable(result) else result
            for widget in widgets:
                existing = snapshots_by_id.get(widget.widget_id)
                if existing is None or widget.pinned:
                    snapshots_by_id[widget.widget_id] = widget
        except Exception as exc:
            logger.warning("Widget snapshot provider %s failed: %s", name, exc)
    return list(snapshots_by_id.values())


def _ensure_builtin_providers() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    try:
        import core.plugins.pinned_widgets  # noqa: F401
        import core.pending_inputs  # noqa: F401
        import plugins.agents.client  # noqa: F401
    except Exception as exc:
        logger.debug("Failed to load built-in widget snapshot providers: %s", exc)
        return
    _builtins_loaded = True
