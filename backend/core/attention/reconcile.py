"""Background reconciliation for scheduled attention windows."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)


class AttentionReconcileService:
    """Periodically reconcile owner attention against enabled quiet-window schedules."""

    def __init__(self, *, interval_s: int = 60) -> None:
        self.interval_s = interval_s
        self.running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        from core.attention.service import attention_service

        await attention_service.reconcile_owner(settings.DEFAULT_USER_ID)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("AttentionReconcileService started (interval=%ds)", self.interval_s)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AttentionReconcileService stopped")

    async def _poll_loop(self) -> None:
        from core.attention.service import attention_service

        while self.running:
            await asyncio.sleep(self.interval_s)
            if not self.running:
                break
            try:
                await attention_service.reconcile_owner(settings.DEFAULT_USER_ID)
            except Exception:
                logger.exception("Attention reconciliation tick failed")


attention_reconcile_service = AttentionReconcileService()
