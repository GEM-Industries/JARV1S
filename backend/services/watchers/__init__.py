"""Watcher protocol and registry for the Automation Engine."""

from typing import Literal, Protocol, runtime_checkable

TriggerMode = Literal["anticipated", "reactive"]


@runtime_checkable
class Watcher(Protocol):
    """
    Thin, passive data provider for one external source.
    Does not evaluate rules, track timing, or publish events.
    The AutomationService manages its lifecycle.

    trigger_mode controls how the AutomationService processes polled items:
      - "anticipated": items have a computable fire time (e.g. calendar events
        with a ``start`` field). The service uses compute_fire_time → call_later
        timers for point-in-time precision.
      - "reactive": items represent events that already happened (e.g. new
        emails). The service fires immediately on first detection; dedup via
        _fired prevents re-fire on subsequent polls.
    """

    source: str
    trigger_mode: TriggerMode

    async def poll(self) -> list[dict]:
        """Return current items from the external source."""
        ...
