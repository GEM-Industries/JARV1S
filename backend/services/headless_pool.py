"""
Shared concurrency pool for headless (non-session) turns.

Silent automations, prefetch, and SystemPulse escalations all schedule
fire-and-forget agent turns that must not be cancelled by voice barge-in.
This module owns:

  - a semaphore bounding concurrent headless agent runs
  - a task set tracking in-flight tasks so shutdown can join them and so
    they can't be garbage-collected mid-run
  - `schedule()` to dispatch a coroutine onto that pool

Usage:

    pool = HeadlessTurnPool(max_concurrent=5)

    async with pool.semaphore:
        await run_headless_turn(...)

    # fire-and-forget
    pool.schedule(run_headless_turn(...))
"""

import asyncio
import logging
from typing import Any, Awaitable

logger = logging.getLogger(__name__)


class HeadlessTurnPool:
    """Bounded concurrency + task tracking for headless agent turns."""

    def __init__(self, max_concurrent: int):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Schedule a fire-and-forget headless turn. Tracked for shutdown cleanup."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_exception)
        return task

    @staticmethod
    def _log_exception(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("Headless turn failed", exc_info=task.exception())

    @property
    def in_flight(self) -> int:
        return len(self._tasks)
