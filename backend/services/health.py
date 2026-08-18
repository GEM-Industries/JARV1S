"""
IntegrationHealth — centralized failure/recovery tracking for background pollers.

Any service that polls an external source (watchers, future device monitors, etc.)
uses this to get consistent behaviour:
  - Consecutive failure counting per source
  - Log-once on failure transition, log-once on recovery (no spam)
  - Event-bus notification when a source crosses the failure threshold

Data providers (watchers) should raise on API errors, never catch-and-return-empty.
The consumer wraps poll() in a try/except and delegates to IntegrationHealth.
"""

import logging

from services.events import event_bus, Event, EventType

logger = logging.getLogger(__name__)

DEFAULT_FAIL_THRESHOLD = 3


class IntegrationHealth:
    """Track consecutive poll failures per source with log-once state transitions."""

    def __init__(self, owner: str, threshold: int = DEFAULT_FAIL_THRESHOLD):
        self._owner = owner
        self._threshold = threshold
        self._failures: dict[str, int] = {}
        self._healthy: dict[str, bool] = {}

    def record_success(self, source: str) -> None:
        """Record a successful poll. Logs once on recovery."""
        self._failures[source] = 0
        if not self._healthy.get(source, True):
            self._healthy[source] = True
            logger.info("[%s] Watcher '%s' recovered", self._owner, source)

    async def record_failure(self, source: str, error: Exception) -> int:
        """
        Record a failed poll. Returns the consecutive failure count.
        Logs once on first failure. Publishes SYSTEM_WARNING at threshold.
        """
        count = self._failures.get(source, 0) + 1
        self._failures[source] = count

        if self._healthy.get(source, True):
            self._healthy[source] = False
            logger.warning(
                "[%s] Watcher '%s' is failing: %s", self._owner, source, error,
            )
        else:
            logger.debug(
                "[%s] Watcher '%s' still failing (%d): %s",
                self._owner, source, count, error,
            )

        if count == self._threshold:
            await event_bus.publish(Event(
                type=EventType.SYSTEM_WARNING,
                source=self._owner,
                data={
                    "message": (
                        f"'{source}' has failed {count} consecutive polls — "
                        f"{self._owner} may be affected."
                    ),
                    "watcher": source,
                    "consecutive_failures": count,
                },
            ))

        return count

    def is_healthy(self, source: str) -> bool:
        return self._healthy.get(source, True)

    @property
    def status(self) -> dict[str, dict]:
        """Snapshot of all tracked sources — useful for diagnostics."""
        return {
            source: {
                "healthy": self._healthy.get(source, True),
                "consecutive_failures": self._failures.get(source, 0),
            }
            for source in set(self._failures) | set(self._healthy)
        }
