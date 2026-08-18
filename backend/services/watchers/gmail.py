"""GmailWatcher — incremental Gmail inbox sync via historyId cursor.

Uses Gmail's history.list API (the same mechanism as the push adapter) so
only messages that arrive AFTER the cursor was established ever trigger
automations — no backfill floods on first connection or re-auth.

Lifecycle
---------
First poll (no cursor)
    Calls users.getProfile to get the current historyId, persists it as the
    baseline cursor, and returns []. Nothing fires on first connect.

Subsequent polls
    Calls history.list since the cursor. Returns only INBOX messagesAdded
    records, updates the cursor, and returns summaries (capped at
    MAX_NEW_PER_POLL as a safety guard against bursts).

historyId expired (API returns 404)
    Baseline is re-established (same as first poll) and [] is returned.
    Mirrors the push adapter's expiry-recovery behaviour.

Cursor is persisted to the watcher_cursors MongoDB collection so it
survives process restarts.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_NEW_PER_POLL = 5  # Safety cap: never dispatch more than this per tick


class GmailWatcher:
    """
    Polls for newly delivered inbox messages using Gmail's history.list API.

    Each dict has fields matching EmailSummary (id, thread_id, subject,
    sender, snippet, date, labels, is_unread). Rules condition on any field,
    e.g.: {"field": "sender", "op": "contains", "value": "boss@company.com"}
    """

    source = "gmail"
    trigger_mode = "reactive"
    trigger_events = [
        {
            "event": "",
            "description": (
                "Triggers when a new email arrives in your Gmail inbox. "
                "Built-in — no external connection required. "
                "Filter by sender, subject, or labels via conditions."
            ),
        }
    ]
    condition_fields = [
        {"field": "subject", "type": "string"},
        {"field": "sender", "type": "string", "hint": "'Name <email>' format — use contains"},
        {"field": "snippet", "type": "string"},
        {"field": "labels", "type": "string", "hint": "use contains to match a label"},
        {"field": "is_unread", "type": "boolean", "hint": "equals 'true' or 'false'"},
    ]

    async def poll(self) -> list[dict]:
        from core.integrations import integrations
        from core.integrations.manager import NeedsReauth

        try:
            gmail = await integrations.get("gmail")
        except (KeyError, NeedsReauth):
            logger.debug("Gmail integration not configured, skipping poll.")
            return []

        cursor = await self._load_cursor()
        if cursor is None:
            await self._reset_cursor(gmail)
            return []

        new_ids, latest_history_id = await self._fetch_new_message_ids(gmail, cursor)

        if not new_ids:
            if latest_history_id:
                await self._save_cursor(latest_history_id)
            return []

        capped = len(new_ids) > MAX_NEW_PER_POLL
        if capped:
            logger.warning(
                "Gmail watcher: capping %d new messages to %d per poll",
                len(new_ids), MAX_NEW_PER_POLL,
            )
            new_ids = new_ids[:MAX_NEW_PER_POLL]

        from plugins.gmail import _fetch_message_summaries
        summaries = await _fetch_message_summaries(gmail, new_ids)

        # Only advance cursor when all messages were dispatched. When capped,
        # leave cursor unchanged so the next poll re-fetches from the same point.
        # AutomationService._fired dedup skips already-dispatched messages.
        if not capped and latest_history_id:
            await self._save_cursor(latest_history_id)

        return [s.model_dump() for s in summaries]

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _fetch_new_message_ids(
        self, gmail, start_history_id: str
    ) -> tuple[list[str], str | None]:
        """Fetch INBOX messagesAdded since start_history_id.

        Returns (new_message_ids, latest_historyId).
        Returns ([], None) on transient error.
        On 404 (cursor expired), re-establishes baseline and returns ([], None).
        """
        try:
            resp = await gmail.get(
                "/users/me/history",
                params={
                    "startHistoryId": start_history_id,
                    "historyTypes": "messageAdded",
                    "labelId": "INBOX",
                },
            )
            if resp.status_code == 404:
                logger.warning(
                    "Gmail historyId expired (cursor=%s) — re-establishing baseline",
                    start_history_id,
                )
                await self._reset_cursor(gmail)
                return [], None
            resp.raise_for_status()
        except Exception as e:
            logger.warning("Gmail history.list failed: %s", e)
            return [], None

        data = resp.json()
        latest: str | None = data.get("historyId")

        seen: set[str] = set()
        ids: list[str] = []
        for record in data.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added.get("message", {}).get("id")
                if msg_id and msg_id not in seen:
                    seen.add(msg_id)
                    ids.append(msg_id)

        return ids, latest

    async def _reset_cursor(self, gmail) -> None:
        """Get the current historyId from users.getProfile and save it.

        Establishes the baseline for future incremental syncs.
        Does not return or fire any messages.
        """
        try:
            resp = await gmail.get("/users/me/profile")
            resp.raise_for_status()
            history_id = str(resp.json()["historyId"])
            await self._save_cursor(history_id)
            logger.info("Gmail watcher: baseline cursor established (historyId=%s)", history_id)
        except Exception as e:
            logger.warning("Gmail watcher: failed to establish baseline cursor: %s", e)

    async def _load_cursor(self) -> str | None:
        from services.database.mongodb import mongodb
        try:
            doc = await mongodb.db.watcher_cursors.find_one({"source": self.source})
            return doc["history_id"] if doc else None
        except Exception as e:
            logger.warning("Gmail watcher: failed to load cursor: %s", e)
            return None

    async def _save_cursor(self, history_id: str) -> None:
        from services.database.mongodb import mongodb
        try:
            await mongodb.db.watcher_cursors.update_one(
                {"source": self.source},
                {"$set": {"history_id": history_id, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception as e:
            logger.warning("Gmail watcher: failed to save cursor: %s", e)
